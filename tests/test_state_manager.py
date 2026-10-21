import json
import subprocess
from datetime import timedelta

import pytest

from tests.conftest import make_article
from typo3_publisher.config import RetryPolicy, StateSettings
from typo3_publisher.models import ArticleStatus, PublicationStage, iso, utcnow
from typo3_publisher.state_manager import (
    EphemeralSync,
    GitBranchSync,
    LocalSync,
    LockHeldError,
    StateError,
    StateStore,
)


def test_article_roundtrip(store):
    article = make_article()
    article.publication.stage = PublicationStage.SAVED
    article.publication.typo3_uid = 42
    article.add_error("typo3", "boom")
    store.save_article(article)
    loaded = store.load_article(article.id)
    assert loaded.content == article.content
    assert loaded.publication.stage is PublicationStage.SAVED
    assert loaded.publication.typo3_uid == 42
    assert loaded.errors[0].message == "boom"


def test_list_by_status_and_corrupt_file_is_skipped(store):
    store.save_article(make_article("a1", status=ArticleStatus.APPROVED))
    store.save_article(make_article("a2", status=ArticleStatus.PUBLISHED))
    (store.root / "articles" / "broken.json").write_text("{not json")
    assert [a.id for a in store.list_articles(ArticleStatus.APPROVED)] == ["a1"]
    assert len(store.list_articles()) == 2


def test_invalid_article_id_rejected(store):
    with pytest.raises(StateError):
        store.load_article("../etc/passwd")


def test_slots_backlog_images(store):
    store.set_slot("k1", status="claimed", run_id="r")
    store.set_slot("k1", status="published")
    assert store.get_slot("k1")["status"] == "published" and store.get_slot("k1")["run_id"] == "r"
    item = store.add_backlog_item("Idea", "Technology")
    store.mark_backlog_item(item["id"], "used", "a1")
    assert store.backlog()[0]["status"] == "used"
    store.register_image("pixabay", "7", "sha", "a1")
    assert store.image_is_used("pixabay", "7")
    assert store.image_is_used("pixabay", "8", "sha")
    assert not store.image_is_used("pixabay", "8", "other")


def test_local_lock(tmp_path):
    settings = StateSettings(lock_ttl_minutes=10)
    a, b = LocalSync(tmp_path, settings), LocalSync(tmp_path, settings)
    a.acquire_lock("run-a")
    with pytest.raises(LockHeldError):
        b.acquire_lock("run-b")
    a.release_lock("run-a")
    b.acquire_lock("run-b")


def test_expired_lock_can_be_taken_over(tmp_path):
    (tmp_path / "lock.json").write_text(json.dumps({"run_id": "dead", "expires_at": iso(utcnow() - timedelta(1))}))
    LocalSync(tmp_path, StateSettings()).acquire_lock("new")
    assert json.loads((tmp_path / "lock.json").read_text())["run_id"] == "new"


def test_ephemeral_sync_copies_but_never_touches_source(tmp_path):
    src = StateStore(tmp_path / "src")
    src.save_article(make_article("a1"))
    eph = EphemeralSync(src.root, StateSettings())
    copy = StateStore(eph.root)
    copy.save_article(make_article("a2"))
    assert [a.id for a in copy.list_articles()] == ["a1", "a2"]
    assert [a.id for a in src.list_articles()] == ["a1"]


# ------------------------------------------------------------------ git branch backend


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True
    )


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    clones = []
    for name in ("runner1", "runner2"):
        path = tmp_path / name
        git(tmp_path, "clone", "-q", str(remote), str(path))
        (path / "README.md").write_text("code")
        git(path, "add", "-A")
        git(path, "commit", "-qm", "init", "--allow-empty")
        clones.append(path)
    git(clones[0], "push", "-q", "origin", "HEAD:main")
    return remote, clones


def test_git_branch_state_survives_runners_and_lock_is_exclusive(repos):
    remote, (r1, r2) = repos
    settings = StateSettings(backend="git_branch", git_branch="bot-state", git_dir=".bot-state", lock_ttl_minutes=30)
    retry = RetryPolicy(attempts=3, base_delay_seconds=0, max_delay_seconds=0)

    s1 = GitBranchSync(r1, settings, retry)
    s1.prepare()  # creates the orphan state branch
    s1.acquire_lock("run-1")
    StateStore(s1.root).save_article(make_article("a1", status=ArticleStatus.PUBLISHED))
    s1.commit("save a1")

    s2 = GitBranchSync(r2, settings, retry)
    s2.prepare()
    assert [a.id for a in StateStore(s2.root).list_articles()] == ["a1"]
    with pytest.raises(LockHeldError):
        s2.acquire_lock("run-2")

    s1.release_lock("run-1")
    s2.acquire_lock("run-2")
    StateStore(s2.root).save_article(make_article("a2"))
    s2.commit("save a2")
    s2.release_lock("run-2")

    s1.prepare()
    assert [a.id for a in StateStore(s1.root).list_articles()] == ["a1", "a2"]
    log = subprocess.run(["git", "log", "--format=%s", "bot-state"], cwd=remote, capture_output=True, text=True).stdout
    assert "[skip ci]" in log
    branches = subprocess.run(["git", "branch"], cwd=remote, capture_output=True, text=True).stdout
    assert "bot-state" in branches and "main" in branches


def test_dry_run_reads_state_branch_without_writing(repos):
    from typo3_publisher.state_manager import create_sync

    remote, (r1, r2) = repos
    settings = StateSettings(backend="git_branch")
    retry = RetryPolicy(attempts=1, base_delay_seconds=0, max_delay_seconds=0)
    # No state branch yet: a dry run must not create it.
    create_sync(settings, retry, r2, dry_run=True).prepare()
    branches = subprocess.run(["git", "branch"], cwd=remote, capture_output=True, text=True).stdout
    assert "bot-state" not in branches

    s1 = GitBranchSync(r1, settings, retry)
    s1.prepare()
    StateStore(s1.root).save_article(make_article("a1"))
    s1.commit("save a1")
    dry = create_sync(settings, retry, r2, dry_run=True)
    assert [a.id for a in StateStore(dry.root).list_articles()] == ["a1"]
    StateStore(dry.root).save_article(make_article("a2"))
    dry.commit("ignored")
    s1.prepare()
    assert [a.id for a in StateStore(s1.root).list_articles()] == ["a1"]
