"""Schedule -> job materialization.

The single most testable piece of the system, so it is kept free of any I/O:
give it a schedule spec and "now", get back the jobs that are due. No database,
no network, no globals.

Weekday convention (one convention, used everywhere):
    0 = Monday ... 6 = Sunday   (Python's ``datetime.weekday()``)

All datetimes are timezone-aware. Storage is UTC; schedules are authored in
Asia/Dhaka. Bangladesh has no DST, so ``+06:00`` never shifts — but we still go
through ``zoneinfo`` so that a future DST change would not silently break.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc

#: How far either side of "now" a start time is considered due. GitHub Actions
#: fires cron at best every 5 minutes and is known to run late, so the window
#: has to reach *backwards* as well as forwards: a tick that arrives 7 minutes
#: late must still start the run, and a forward-only window would silently drop
#: it. The DB's unique(schedule_id, planned_start_at) is what actually prevents
#: double-dispatch — two ticks may both find the same job and the second insert
#: is ignored — so this window only controls *how late* a run may start and
#: still be useful.
DEFAULT_HORIZON_MINUTES = 10


@dataclass(frozen=True)
class ScheduleSpec:
    """A weekly schedule, exactly as authored in Asia/Dhaka local time."""

    id: str
    days_of_week: tuple[int, ...]
    start_time: time
    duration_minutes: int
    stop_at_time: time | None = None
    timezone: str = "Asia/Dhaka"
    start_date: date | None = None
    end_date: date | None = None

    def __post_init__(self) -> None:
        if not self.days_of_week:
            raise ValueError("days_of_week must not be empty")
        if any(d < 0 or d > 6 for d in self.days_of_week):
            raise ValueError("days_of_week must be 0 (Monday) .. 6 (Sunday)")
        if self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")


@dataclass(frozen=True)
class DueJob:
    """One planned run, resolved to absolute UTC instants."""

    schedule_id: str
    planned_start_at: datetime
    planned_stop_at: datetime
    stop_reason_pref: str  # "duration" | "clock_time" — whichever comes first

    @property
    def timeout_seconds(self) -> int:
        """Value for Kaggle's ``session_timeout_seconds``.

        The +300s buffer means Kaggle's own limit never fires before our
        watchdog does — the watchdog is the intended stop, Kaggle's timeout is
        the backstop for when the notebook ignores us entirely.
        """
        span = int((self.planned_stop_at - self.planned_start_at).total_seconds())
        return span + 300


def _resolve_stop(start_utc: datetime, spec: ScheduleSpec, tz: ZoneInfo) -> tuple[datetime, str]:
    """Stop instant = whichever comes first: duration, or the clock time."""
    by_duration = start_utc + timedelta(minutes=spec.duration_minutes)

    if spec.stop_at_time is None:
        return by_duration, "duration"

    start_local = start_utc.astimezone(tz)
    stop_local = datetime.combine(start_local.date(), spec.stop_at_time, tzinfo=tz)
    if stop_local <= start_local:
        # A stop time at/behind the start time means the run crosses midnight.
        stop_local += timedelta(days=1)
    by_clock = stop_local.astimezone(UTC)

    if by_clock <= by_duration:
        return by_clock, "clock_time"
    return by_duration, "duration"


def materialize(
    spec: ScheduleSpec,
    now_utc: datetime,
    horizon_minutes: int = DEFAULT_HORIZON_MINUTES,
) -> list[DueJob]:
    """Return the jobs of ``spec`` whose start falls in ``[now-h, now+h]``.

    The window is symmetric deliberately. Ticks are not punctual, so a start
    time slightly in the past is still a job that has not been started yet and
    must be honoured; a forward-only window would drop exactly those runs.

    Checks "yesterday" and "tomorrow" as well, so an overnight schedule
    (start 23:00, stop 02:00) still produces the correct stop instant for the
    day it started, and a schedule at 00:05 is caught by the tick that runs
    just before midnight.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")

    tz = ZoneInfo(spec.timezone)
    now_local = now_utc.astimezone(tz)
    horizon = timedelta(minutes=horizon_minutes)
    window_start = now_utc - horizon
    window_end = now_utc + horizon

    due: list[DueJob] = []
    for day_offset in (-1, 0, 1):
        day = (now_local + timedelta(days=day_offset)).date()

        if day.weekday() not in spec.days_of_week:
            continue
        if spec.start_date is not None and day < spec.start_date:
            continue
        if spec.end_date is not None and day > spec.end_date:
            continue

        start_local = datetime.combine(day, spec.start_time, tzinfo=tz)
        start_utc = start_local.astimezone(UTC)

        if not (window_start <= start_utc <= window_end):
            continue

        stop_utc, reason = _resolve_stop(start_utc, spec, tz)
        due.append(
            DueJob(
                schedule_id=spec.id,
                planned_start_at=start_utc,
                planned_stop_at=stop_utc,
                stop_reason_pref=reason,
            )
        )

    due.sort(key=lambda j: j.planned_start_at)
    return due


def is_past_grace(job_start_utc: datetime, now_utc: datetime, grace_minutes: int) -> bool:
    """True when a still-undispatched job is too late to be worth starting.

    Starting a 6-hour GPU run half a day late is worse than not starting it, so
    past the grace window the job is marked ``missed`` instead.
    """
    return now_utc > job_start_utc + timedelta(minutes=grace_minutes)