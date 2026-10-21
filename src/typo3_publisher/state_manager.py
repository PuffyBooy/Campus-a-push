"""Durable article registry and run state.

Layout of the state directory (one JSON file per article keeps git merges trivial)::

    articles/<article-id>.json   full article record incl. revisions, reviews, versions
    slots.json                   claimed publishing slots (duplicate-run protection)
    backlog.json                 manually supplied ideas
    used_images.json             image ids / hashes already used
    runs/<YYYY-MM>.jsonl         one summary line per run
    lock.json                    lease lock held by the running workflow

In GitHub Actions the directory is a worktree of a dedicated ``bot-state`` branch
(:class:`GitBranchSync`). Every checkpoint is committed and pushed, so state survives
the ephemeral runner. Pushes are fast-forward only, which makes the lock a real
compare-and-swap: two runs cannot both acquire it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import RetryPolicy, StateSettings
from .logging_utils import get_logger
from .models import Article, ArticleStatus, iso, parse_iso, utcnow
from .retry import backoff_delay, sleep
from .serialization import from_dict, to_dict

log = get_logger("state")


class StateError(RuntimeError):
    pass


class LockHeldError(StateError):
    pass


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


class StateStore:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "articles").mkdir(exist_ok=True)

    # ------------------------------------------------------------------ local locking
    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Serialises writers on the same machine (e.g. two local CLI invocations)."""
        lock_path = self.root / ".local.lock"
        with lock_path.open("a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ articles
    def _article_path(self, article_id: str) -> Path:
        if not article_id or "/" in article_id or article_id.startswith("."):
            raise StateError(f"invalid article id {article_id!r}")
        return self.root / "articles" / f"{article_id}.json"

    def save_article(self, article: Article) -> None:
        article.touch()
        with self.transaction():
            _atomic_write_json(self._article_path(article.id), to_dict(article))

    def load_article(self, article_id: str) -> Article:
        path = self._article_path(article_id)
        if not path.exists():
            raise StateError(f"article {article_id} not found")
        return from_dict(Article, _read_json(path, {}), strict=False)

    def list_articles(self, *statuses: ArticleStatus) -> list[Article]:
        articles = []
        for path in sorted((self.root / "articles").glob("*.json")):
            try:
                article = from_dict(Article, _read_json(path, {}), strict=False)
            except Exception as exc:  # corrupt file must not stop the bot
                log.error("cannot read %s: %s", path.name, exc)
                continue
            if not statuses or article.status in statuses:
                articles.append(article)
        return sorted(articles, key=lambda a: (a.created_at, a.id))

    # ------------------------------------------------------------------ research material
    def save_research(self, article_id: str, documents: list[dict[str, Any]]) -> None:
        self._article_path(article_id)  # validates the id
        with self.transaction():
            _atomic_write_json(self.root / "research" / f"{article_id}.json", documents)

    def load_research(self, article_id: str) -> list[dict[str, Any]]:
        self._article_path(article_id)
        return _read_json(self.root / "research" / f"{article_id}.json", [])

    # ------------------------------------------------------------------ slots
    def slots(self) -> dict[str, dict[str, Any]]:
        return _read_json(self.root / "slots.json", {})

    def get_slot(self, key: str) -> dict[str, Any] | None:
        return self.slots().get(key)

    def set_slot(self, key: str, **data: Any) -> None:
        with self.transaction():
            slots = self.slots()
            entry = slots.get(key, {})
            entry.update(data)
            entry.setdefault("claimed_at", iso())
            slots[key] = entry
            # keep the file small: forget slots older than ~60 days
            if len(slots) > 400:
                for old in sorted(slots)[: len(slots) - 400]:
                    slots.pop(old, None)
            _atomic_write_json(self.root / "slots.json", slots)

    # ------------------------------------------------------------------ backlog
    def backlog(self) -> list[dict[str, Any]]:
        return _read_json(self.root / "backlog.json", [])

    def add_backlog_item(self, title: str, topic: str = "", notes: str = "") -> dict[str, Any]:
        with self.transaction():
            items = self.backlog()
            item = {
                "id": f"idea-{len(items) + 1:04d}-{utcnow():%Y%m%d%H%M%S}",
                "title": title,
                "topic": topic,
                "notes": notes,
                "status": "pending",
                "added_at": iso(),
            }
            items.append(item)
            _atomic_write_json(self.root / "backlog.json", items)
        return item

    def mark_backlog_item(self, item_id: str, status: str, article_id: str = "") -> None:
        with self.transaction():
            items = self.backlog()
            for item in items:
                if item["id"] == item_id:
                    item["status"] = status
                    item["article_id"] = article_id
                    item["updated_at"] = iso()
            _atomic_write_json(self.root / "backlog.json", items)

    def used_config_backlog(self) -> set[str]:
        return set(_read_json(self.root / "config_backlog_used.json", []))

    def mark_config_backlog_used(self, key: str) -> None:
        with self.transaction():
            used = sorted(self.used_config_backlog() | {key})
            _atomic_write_json(self.root / "config_backlog_used.json", used)

    # ------------------------------------------------------------------ images
    def used_images(self) -> dict[str, dict[str, Any]]:
        return _read_json(self.root / "used_images.json", {})

    def register_image(self, provider: str, provider_id: str, sha256: str, article_id: str) -> None:
        with self.transaction():
            data = self.used_images()
            data[f"{provider}:{provider_id}"] = {"sha256": sha256, "article_id": article_id, "at": iso()}
            _atomic_write_json(self.root / "used_images.json", data)

    def image_is_used(self, provider: str, provider_id: str, sha256: str = "") -> bool:
        data = self.used_images()
        if f"{provider}:{provider_id}" in data:
            return True
        return bool(sha256) and any(v.get("sha256") == sha256 for v in data.values())

    # ------------------------------------------------------------------ runs
    def record_run(self, summary: dict[str, Any]) -> None:
        path = self.root / "runs" / f"{utcnow():%Y-%m}.jsonl"
        path.parent.mkdir(exist_ok=True)
        with self.transaction(), path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")

    def write_json(self, name: str, data: Any) -> None:
        with self.transaction():
            _atomic_write_json(self.root / name, data)


# ====================================================================== synchronisation


class StateSync:
    """Interface: make state durable and mutually exclusive across runs."""

    root: Path

    def prepare(self) -> None: ...

    def commit(self, message: str) -> None: ...

    def acquire_lock(self, run_id: str) -> None: ...

    def release_lock(self, run_id: str) -> None: ...


class LocalSync(StateSync):
    """State in a local directory (development, self-hosted runners with persistent disk)."""

    def __init__(self, root: Path, settings: StateSettings):
        self.root = Path(root)
        self.settings = settings

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def commit(self, message: str) -> None:
        log.debug("local state checkpoint: %s", message)

    def acquire_lock(self, run_id: str) -> None:
        _acquire_lock_file(self.root / "lock.json", run_id, self.settings.lock_ttl_minutes)

    def release_lock(self, run_id: str) -> None:
        _release_lock_file(self.root / "lock.json", run_id)


class EphemeralSync(LocalSync):
    """Used for dry runs: a throw-away copy of the state, never committed."""

    def __init__(self, source: Path | None, settings: StateSettings):
        tmp = Path(tempfile.mkdtemp(prefix="typo3-bot-dryrun-state-"))
        if source and source.exists():
            shutil.copytree(source, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "lock.json"))
        super().__init__(tmp, settings)


def _lock_is_active(data: dict[str, Any]) -> bool:
    expires = parse_iso(data.get("expires_at"))
    return bool(expires and expires > utcnow())


def _acquire_lock_file(path: Path, run_id: str, ttl_minutes: int) -> None:
    existing = _read_json(path, {})
    if existing and existing.get("run_id") != run_id and _lock_is_active(existing):
        raise LockHeldError(f"state is locked by run {existing.get('run_id')} until {existing.get('expires_at')}")
    _atomic_write_json(path, _lock_payload(run_id, ttl_minutes))


def _release_lock_file(path: Path, run_id: str) -> bool:
    existing = _read_json(path, {})
    if existing.get("run_id") == run_id:
        path.unlink(missing_ok=True)
        return True
    return False


def _lock_payload(run_id: str, ttl_minutes: int) -> dict[str, Any]:
    now = utcnow()
    return {
        "run_id": run_id,
        "host": socket.gethostname(),
        "acquired_at": iso(now),
        "expires_at": iso(now + timedelta(minutes=ttl_minutes)),
    }


class GitBranchSync(StateSync):
    """Keeps state on a dedicated branch, checked out as a git worktree.

    ``repo_dir`` is the main checkout (which has the remote configured).
    """

    def __init__(self, repo_dir: Path, settings: StateSettings, retry: RetryPolicy):
        self.repo_dir = Path(repo_dir)
        self.settings = settings
        self.retry = retry
        self.root = (self.repo_dir / settings.git_dir).resolve()
        self.branch = settings.git_branch
        self.remote = settings.git_remote

    # -------------------------------------------------------------- git helpers
    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [
            "git",
            "-c",
            f"user.name={self.settings.commit_author_name}",
            "-c",
            f"user.email={self.settings.commit_author_email}",
            *args,
        ]
        result = subprocess.run(cmd, cwd=cwd or self.root, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise StateError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:500]}")
        return result

    def _remote_branch_exists(self) -> bool:
        res = self._git("ls-remote", "--heads", self.remote, self.branch, cwd=self.repo_dir, check=False)
        return res.returncode == 0 and bool(res.stdout.strip())

    def prepare(self, create: bool = True) -> None:
        """Check out (or refresh) the state branch. ``create=False`` never creates or pushes it."""
        if (self.root / ".git").exists():
            self._sync_from_remote()
            return
        if not create and not self._remote_branch_exists():
            return
        if self._remote_branch_exists():
            self._git(
                "fetch", self.remote, f"{self.branch}:refs/remotes/{self.remote}/{self.branch}", cwd=self.repo_dir
            )
            self._git(
                "worktree", "add", "-B", self.branch, str(self.root), f"{self.remote}/{self.branch}", cwd=self.repo_dir
            )
        else:
            log.info("state branch %s does not exist yet; creating it", self.branch)
            self._git("worktree", "add", "--detach", str(self.root), cwd=self.repo_dir)
            self._git("checkout", "--orphan", self.branch)
            self._git("rm", "-rf", "--quiet", ".", check=False)
            for leftover in self.root.iterdir():
                if leftover.name != ".git":
                    shutil.rmtree(leftover) if leftover.is_dir() else leftover.unlink()
            (self.root / "README.md").write_text(
                "# Bot state\n\nMachine-written state of the TYPO3 publishing bot.\n"
                "Do not edit while a run is active.\n",
                encoding="utf-8",
            )
            self._git("add", "-A")
            self._git("commit", "-m", "Initialise bot state [skip ci]")
            self._push()

    def _sync_from_remote(self) -> None:
        if self._remote_branch_exists():
            self._git("fetch", self.remote, self.branch)
            self._git("reset", "--hard", f"{self.remote}/{self.branch}")

    def _push(self) -> bool:
        res = self._git("push", self.remote, f"HEAD:refs/heads/{self.branch}", check=False)
        if res.returncode != 0:
            log.warning("push of state branch rejected: %s", res.stderr.strip()[:300])
        return res.returncode == 0

    def _has_changes(self) -> bool:
        return bool(self._git("status", "--porcelain").stdout.strip())

    def commit(self, message: str) -> None:
        if not self._has_changes():
            return
        self._git("add", "-A")
        self._git("commit", "-m", f"{message} [skip ci]")
        for attempt in range(1, self.retry.attempts + 1):
            if self._push():
                return
            # Someone else pushed (should not happen while we hold the lock, but be safe).
            res = self._git("pull", "--rebase", self.remote, self.branch, check=False)
            if res.returncode != 0:
                self._git("rebase", "--abort", check=False)
                raise StateError(f"cannot rebase state branch: {res.stderr.strip()[:300]}")
            sleep(backoff_delay(attempt, self.retry))
        raise StateError("could not push state after retries")

    # -------------------------------------------------------------- lease lock (CAS via fast-forward push)
    def acquire_lock(self, run_id: str) -> None:
        lock_path = self.root / "lock.json"
        for attempt in range(1, self.retry.attempts + 1):
            self._sync_from_remote()
            existing = _read_json(lock_path, {})
            if existing and existing.get("run_id") != run_id and _lock_is_active(existing):
                raise LockHeldError(
                    f"state is locked by run {existing.get('run_id')} until {existing.get('expires_at')}"
                )
            _atomic_write_json(lock_path, _lock_payload(run_id, self.settings.lock_ttl_minutes))
            self._git("add", "-A")
            self._git("commit", "-m", f"Acquire lock for run {run_id} [skip ci]")
            if self._push():
                log.info("acquired state lock for run %s", run_id)
                return
            # Lost the race: another run pushed first. Re-read and re-evaluate.
            sleep(backoff_delay(attempt, self.retry))
        raise LockHeldError("could not acquire state lock (concurrent run?)")

    def release_lock(self, run_id: str) -> None:
        try:
            self._sync_from_remote()
            if _release_lock_file(self.root / "lock.json", run_id):
                self.commit(f"Release lock for run {run_id}")
        except Exception as exc:  # the lock expires on its own
            log.warning("could not release state lock: %s", exc)


def create_sync(settings_state: StateSettings, retry: RetryPolicy, base_dir: Path, *, dry_run: bool) -> StateSync:
    local_root = base_dir / settings_state.local_dir
    if dry_run:
        source = local_root
        if settings_state.backend == "git_branch":
            git_sync = GitBranchSync(base_dir, settings_state, retry)
            try:  # read the current state so the dry run sees real history; never pushes
                git_sync.prepare(create=False)
            except StateError as exc:
                log.warning("dry run without state branch: %s", exc)
            source = git_sync.root
        return EphemeralSync(source, settings_state)
    if settings_state.backend == "git_branch":
        return GitBranchSync(base_dir, settings_state, retry)
    return LocalSync(local_root, settings_state)
