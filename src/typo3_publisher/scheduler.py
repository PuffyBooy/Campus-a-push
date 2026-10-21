"""Timezone-aware publishing slots.

GitHub Actions cron runs in UTC, can start late, and occasionally runs twice. The
workflow therefore triggers often (e.g. every 30 minutes on weekdays) and this module
decides whether a configured local slot (e.g. 08:00 Europe/Vienna) is due:

* a slot is *due* from its local start time until ``slot_grace_minutes`` later,
* every slot is claimed in the persistent state before any work starts, so a delayed,
  retried or overlapping run cannot publish for the same slot twice,
* with ``catch_up: latest_only`` only the most recent missed slot is served.

Daylight saving time is handled by ``zoneinfo``: slot times are local wall-clock times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import PublishingSettings

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Slot:
    key: str
    local_start: datetime

    @property
    def utc_start(self) -> datetime:
        return self.local_start.astimezone(ZoneInfo("UTC"))


def _parse_time(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def _normalise_day(day: str) -> str:
    d = day.strip().lower()[:3]
    if d not in DAY_NAMES:
        raise ValueError(f"invalid day name {day!r}")
    return d


def slot_key(local_start: datetime, tz_name: str) -> str:
    return f"{local_start:%Y-%m-%dT%H:%M}@{tz_name}"


def slots_on(day: date, settings: PublishingSettings) -> list[Slot]:
    tz = ZoneInfo(settings.timezone)
    weekday = DAY_NAMES[day.weekday()]
    result: dict[str, Slot] = {}
    for rule in settings.slots:
        if weekday not in {_normalise_day(d) for d in rule.days}:
            continue
        for t in rule.times:
            local = datetime.combine(day, _parse_time(t), tzinfo=tz)
            # Normalise non-existent local times (spring-forward gap) via a UTC round trip.
            local = local.astimezone(ZoneInfo("UTC")).astimezone(tz)
            key = slot_key(datetime.combine(day, _parse_time(t)), settings.timezone)
            result[key] = Slot(key=key, local_start=local)
    return sorted(result.values(), key=lambda s: s.local_start)


def due_slots(now: datetime, settings: PublishingSettings) -> list[Slot]:
    """Slots whose window [start, start+grace) contains ``now`` (aware datetime)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    tz = ZoneInfo(settings.timezone)
    local_now = now.astimezone(tz)
    grace = timedelta(minutes=settings.slot_grace_minutes)
    candidates = []
    # Look at yesterday too, in case a grace window crosses midnight.
    for offset in (-1, 0):
        for slot in slots_on(local_now.date() + timedelta(days=offset), settings):
            if slot.local_start <= local_now < slot.local_start + grace:
                candidates.append(slot)
    return sorted(candidates, key=lambda s: s.local_start)


def select_slot(
    now: datetime, settings: PublishingSettings, claimed: dict[str, dict]
) -> tuple[Slot | None, list[Slot]]:
    """Return (slot to serve now, earlier due-but-unserved slots that should be marked skipped)."""
    open_slots = [s for s in due_slots(now, settings) if s.key not in claimed]
    if not open_slots:
        return None, []
    if settings.catch_up == "latest_only":
        return open_slots[-1], open_slots[:-1]
    # catch_up == "none": only serve a slot if it is the most recent due slot overall
    latest_due = due_slots(now, settings)[-1]
    if open_slots[-1].key == latest_due.key:
        return latest_due, open_slots[:-1]
    return None, open_slots


def upcoming_slots(now: datetime, settings: PublishingSettings, days: int) -> list[Slot]:
    tz = ZoneInfo(settings.timezone)
    local_now = now.astimezone(tz)
    result = []
    for offset in range(days + 1):
        for slot in slots_on(local_now.date() + timedelta(days=offset), settings):
            if slot.local_start >= local_now:
                result.append(slot)
    return result


def suggest_cron(settings: PublishingSettings, minutes: str = "7,37") -> list[str]:
    """UTC cron expressions that cover all slots in both winter and summer time.

    Example for 08/12/17 Europe/Vienna on weekdays → ``7,37 6-16 * * *`` (weekday filtering
    happens in Python, in local time, so the UTC day-of-week does not matter).
    """
    tz = ZoneInfo(settings.timezone)
    hours: set[int] = set()
    grace_hours = max(1, settings.slot_grace_minutes // 60)
    for probe in (date(2025, 1, 15), date(2025, 7, 15)):  # winter / summer offsets
        for rule in settings.slots:
            for t in rule.times:
                local = datetime.combine(probe, _parse_time(t), tzinfo=tz)
                utc_hour = local.astimezone(ZoneInfo("UTC")).hour
                for h in range(utc_hour, utc_hour + grace_hours):
                    hours.add(h % 24)
    ordered = sorted(hours)
    return [f"{minutes} {','.join(str(h) for h in ordered)} * * *"]
