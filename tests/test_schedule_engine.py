"""Weekly schedule -> jobs, plus stop decisions. The core logic under test."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from runner.schedule_engine import (
    DueJob,
    ScheduleSpec,
    is_past_grace,
    materialize,
)

DHAKA = ZoneInfo("Asia/Dhaka")
UTC = timezone.utc


def dhaka(y, m, d, hh, mm=0):
    """A wall-clock instant in Asia/Dhaka, as the user would have typed it."""
    return datetime(y, m, d, hh, mm, tzinfo=DHAKA)


def spec(**kw) -> ScheduleSpec:
    base = dict(
        id="s1",
        days_of_week=(0,),          # Monday
        start_time=time(22, 0),     # 10 PM Dhaka
        duration_minutes=120,
    )
    base.update(kw)
    return ScheduleSpec(**base)


# ---- which days are due --------------------------------------------------


def test_fires_on_the_selected_weekday():
    # 2026-09-28 is a Monday. 22:00 Dhaka == 16:00 UTC.
    now = datetime(2026, 9, 28, 15, 58, tzinfo=UTC)
    jobs = materialize(spec(), now)
    assert len(jobs) == 1
    assert jobs[0].planned_start_at == datetime(2026, 9, 28, 16, 0, tzinfo=UTC)


def test_does_not_fire_on_an_unselected_weekday():
    # 2026-09-29 is a Tuesday.
    now = datetime(2026, 9, 29, 15, 58, tzinfo=UTC)
    assert materialize(spec(), now) == []


def test_does_not_fire_outside_the_horizon():
    # 20 minutes early, default horizon is 10.
    now = datetime(2026, 9, 28, 15, 40, tzinfo=UTC)
    assert materialize(spec(), now) == []


def test_fires_when_the_tick_itself_is_late():
    """GitHub cron can run late; a late tick must still catch the job."""
    now = datetime(2026, 9, 28, 16, 7, tzinfo=UTC)  # 7 min after the start
    jobs = materialize(spec(), now)
    assert len(jobs) == 1


def test_does_not_refire_once_the_horizon_has_passed():
    now = datetime(2026, 9, 28, 16, 30, tzinfo=UTC)
    assert materialize(spec(), now) == []


def test_multiple_days_in_one_schedule():
    s = spec(days_of_week=(0, 2, 4))  # Mon, Wed, Fri
    for day in (28, 30):  # Sep 28 Mon, Sep 30 Wed
        now = datetime(2026, 9, day, 15, 58, tzinfo=UTC)
        assert len(materialize(s, now)) == 1, day
    assert materialize(s, datetime(2026, 9, 29, 15, 58, tzinfo=UTC)) == []


# ---- stop time -----------------------------------------------------------


def test_stop_is_start_plus_duration_by_default():
    jobs = materialize(spec(duration_minutes=180), datetime(2026, 9, 28, 15, 58, tzinfo=UTC))
    job = jobs[0]
    assert job.planned_stop_at == job.planned_start_at + timedelta(minutes=180)
    assert job.stop_reason_pref == "duration"


def test_clock_stop_wins_when_it_comes_first():
    """duration 6h would stop at 04:00; the 23:30 clock stop wins."""
    jobs = materialize(
        spec(duration_minutes=360, stop_at_time=time(23, 30)),
        datetime(2026, 9, 28, 15, 58, tzinfo=UTC),
    )
    job = jobs[0]
    assert job.planned_stop_at == dhaka(2026, 9, 28, 23, 30).astimezone(UTC)
    assert job.stop_reason_pref == "clock_time"


def test_duration_wins_when_it_comes_first():
    jobs = materialize(
        spec(duration_minutes=60, stop_at_time=time(23, 30)),
        datetime(2026, 9, 28, 15, 58, tzinfo=UTC),
    )
    job = jobs[0]
    assert job.planned_stop_at == job.planned_start_at + timedelta(minutes=60)
    assert job.stop_reason_pref == "duration"


def test_clock_stop_before_start_time_crosses_midnight():
    """Start 22:00, stop 02:00 — the stop belongs to the *next* day."""
    jobs = materialize(
        spec(start_time=time(22, 0), duration_minutes=600, stop_at_time=time(2, 0)),
        datetime(2026, 9, 28, 15, 58, tzinfo=UTC),
    )
    job = jobs[0]
    assert job.planned_stop_at == dhaka(2026, 9, 29, 2, 0).astimezone(UTC)
    assert job.stop_reason_pref == "clock_time"


def test_a_tick_touching_midnight_still_finds_an_earlier_start():
    """The tick after a 23:55 start lands on the *next* local day.

    This is exactly what the engine's yesterday/today/tomorrow walk is for. At
    00:03 Dhaka on Tuesday the local date is Tuesday (weekday 1), so a
    Monday-only schedule is reachable *only* through the yesterday branch —
    without it, every schedule close to midnight would silently never fire.
    """
    jobs = materialize(
        spec(days_of_week=(0,), start_time=time(23, 55), duration_minutes=240),
        dhaka(2026, 9, 29, 0, 3).astimezone(UTC),  # Tue 00:03 Dhaka = Mon 18:03 UTC
    )
    assert len(jobs) == 1
    assert jobs[0].planned_start_at == dhaka(2026, 9, 28, 23, 55).astimezone(UTC)


# ---- validity window -----------------------------------------------------


def test_start_date_excludes_earlier_days():
    s = spec(start_date=date(2026, 10, 1))
    assert materialize(s, datetime(2026, 9, 28, 15, 58, tzinfo=UTC)) == []


def test_end_date_excludes_later_days():
    s = spec(end_date=date(2026, 9, 1))
    assert materialize(s, datetime(2026, 9, 28, 15, 58, tzinfo=UTC)) == []


# ---- timeout -------------------------------------------------------------


def test_timeout_carries_a_buffer_over_the_deadline():
    """Kaggle's limit must sit *above* our deadline, never below it."""
    jobs = materialize(spec(duration_minutes=120), datetime(2026, 9, 28, 15, 58, tzinfo=UTC))
    job = jobs[0]
    assert job.timeout_seconds == 120 * 60 + 300


# ---- validation ----------------------------------------------------------


def test_empty_day_list_is_rejected():
    with pytest.raises(ValueError):
        spec(days_of_week=())


def test_out_of_range_weekday_is_rejected():
    with pytest.raises(ValueError):
        spec(days_of_week=(7,))


def test_zero_duration_is_rejected():
    with pytest.raises(ValueError):
        spec(duration_minutes=0)


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        materialize(spec(), datetime(2026, 9, 28, 16, 0))


# ---- grace ---------------------------------------------------------------


def test_grace_not_exceeded_within_window():
    start = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)
    assert not is_past_grace(start, start + timedelta(minutes=20), grace_minutes=30)


def test_grace_exceeded_after_window():
    start = datetime(2026, 9, 28, 16, 0, tzinfo=UTC)
    assert is_past_grace(start, start + timedelta(minutes=31), grace_minutes=30)