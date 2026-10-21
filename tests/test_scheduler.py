from datetime import UTC, datetime

from typo3_publisher.config import PublishingSettings, SlotSettings
from typo3_publisher.scheduler import due_slots, select_slot, slots_on, suggest_cron, upcoming_slots

P = PublishingSettings(
    slots=[SlotSettings(days=["mon", "tue", "wed", "thu", "fri"], times=["08:00", "12:00", "17:00"])],
    slot_grace_minutes=150,
)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def test_summer_and_winter_offsets():
    # 08:00 Vienna = 06:00 UTC in summer (CEST) and 07:00 UTC in winter (CET)
    summer = slots_on(datetime(2026, 7, 15).date(), P)[0]
    winter = slots_on(datetime(2026, 1, 14).date(), P)[0]
    assert summer.utc_start == utc(2026, 7, 15, 6, 0)
    assert winter.utc_start == utc(2026, 1, 14, 7, 0)


def test_dst_switch_days():
    # 2026-03-29 (Sunday) is the spring switch, 2026-10-25 (Sunday) the autumn switch: use the Mondays after.
    assert slots_on(datetime(2026, 3, 30).date(), P)[0].utc_start == utc(2026, 3, 30, 6, 0)
    assert slots_on(datetime(2026, 3, 27).date(), P)[0].utc_start == utc(2026, 3, 27, 7, 0)
    assert slots_on(datetime(2026, 10, 26).date(), P)[0].utc_start == utc(2026, 10, 26, 7, 0)


def test_weekend_has_no_slots():
    assert slots_on(datetime(2026, 10, 24).date(), P) == []
    assert due_slots(utc(2026, 10, 24, 10, 0), P) == []


def test_due_window_and_grace():
    assert due_slots(utc(2026, 10, 21, 5, 59), P) == []  # 07:59 Vienna
    assert [s.key for s in due_slots(utc(2026, 10, 21, 6, 0), P)] == ["2026-10-21T08:00@Europe/Vienna"]
    assert [s.key for s in due_slots(utc(2026, 10, 21, 8, 29), P)] == ["2026-10-21T08:00@Europe/Vienna"]
    assert due_slots(utc(2026, 10, 21, 8, 30), P) == []  # 10:30 Vienna, grace over


def test_delayed_run_serves_slot_once():
    claimed: dict = {}
    slot, skipped = select_slot(utc(2026, 10, 21, 7, 40), P, claimed)  # GitHub ran 1h40 late
    assert slot.key == "2026-10-21T08:00@Europe/Vienna" and skipped == []
    claimed[slot.key] = {"status": "published"}
    assert select_slot(utc(2026, 10, 21, 7, 55), P, claimed) == (None, [])


def test_latest_only_skips_older_overlapping_slots():
    p = PublishingSettings(slots=[SlotSettings(times=["08:00", "09:00"])], slot_grace_minutes=150)
    slot, skipped = select_slot(utc(2026, 10, 21, 7, 30), p, {})  # 09:30 Vienna: both due
    assert slot.key.endswith("T09:00@Europe/Vienna")
    assert [s.key for s in skipped] == ["2026-10-21T08:00@Europe/Vienna"]


def test_upcoming_and_cron():
    upcoming = upcoming_slots(utc(2026, 10, 23, 14, 0), P, 3)  # Friday 16:00 Vienna
    assert [s.key for s in upcoming][:2] == ["2026-10-23T17:00@Europe/Vienna", "2026-10-26T08:00@Europe/Vienna"]
    cron = suggest_cron(P)[0]
    hours = {int(h) for h in cron.split()[1].split(",")}
    assert {6, 7, 10, 11, 15, 16} <= hours  # both offsets of 08/12/17 Vienna
