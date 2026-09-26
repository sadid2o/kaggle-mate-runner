"""tick's decision layer — the part that must never start a run twice."""

from datetime import datetime, time, timedelta, timezone

import pytest

from runner.tick import due_job_rows, emit_github_output, mark_missed, to_spec


def schedule_row(**kw):
    row = {
        "id": "s1",
        "notebook_id": "n1",
        "days_of_week": [0],
        "start_time": "22:00:00",
        "duration_minutes": 120,
        "stop_at_time": None,
        "timezone": "Asia/Dhaka",
        "start_date": None,
        "end_date": None,
        "is_active": True,
        "notebooks": {"id": "n1", "kaggle_ref": "me/nb", "account_id": "a1", "is_active": True},
    }
    row.update(kw)
    return row


MONDAY = datetime(2026, 9, 28, 15, 58, tzinfo=timezone.utc)  # 21:58 Dhaka


# ---- row -> engine spec --------------------------------------------------


def test_postgres_strings_become_real_time_objects():
    """PostgREST sends time/date as strings; the engine must still get objects."""
    spec = to_spec(schedule_row())
    assert spec.start_time == time(22, 0)
    assert spec.days_of_week == (0,)
    assert spec.timezone == "Asia/Dhaka"


def test_null_stop_at_time_stays_none():
    assert to_spec(schedule_row(stop_at_time=None)).stop_at_time is None


def test_stop_at_time_is_parsed_when_present():
    assert to_spec(schedule_row(stop_at_time="23:30:00")).stop_at_time == time(23, 30)


# ---- due jobs ------------------------------------------------------------


def test_due_job_carries_everything_the_worker_needs():
    rows = due_job_rows([schedule_row()], MONDAY, horizon=10)
    assert len(rows) == 1
    job = rows[0]
    assert job["notebook_id"] == "n1"
    assert job["account_id"] == "a1"
    assert job["state"] == "pending"
    assert job["timeout_seconds"] == 120 * 60 + 300
    assert job["stop_reason_pref"] == "duration"
    assert job["planned_start_at"].startswith("2026-09-28T16:00")


def test_inactive_notebook_is_skipped_not_fatal():
    """One deactivated notebook must not stop the whole tick."""
    rows = due_job_rows([schedule_row(notebooks={"id": "n1", "account_id": "a1", "is_active": False})], MONDAY, 10)
    assert rows == []


def test_missing_notebook_is_skipped():
    rows = due_job_rows([schedule_row(notebooks=None)], MONDAY, 10)
    assert rows == []


def test_malformed_schedule_is_skipped_not_fatal():
    rows = due_job_rows([schedule_row(start_time="not-a-time")], MONDAY, 10)
    assert rows == []


def test_a_bad_row_does_not_block_a_good_one():
    rows = due_job_rows(
        [schedule_row(id="bad", start_time="nope"), schedule_row(id="good")],
        MONDAY,
        10,
    )
    assert len(rows) == 1


def test_nothing_due_returns_empty():
    assert due_job_rows([schedule_row()], datetime(2026, 9, 29, 15, 58, tzinfo=timezone.utc), 10) == []


# ---- github output -------------------------------------------------------


def test_github_output_uses_mode_prefixed_tasks(tmp_path, monkeypatch):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    emit_github_output(["start:abc"])
    assert out.read_text().strip() == 'tasks=["start:abc"]'


def test_github_output_emits_a_sentinel_when_idle(tmp_path, monkeypatch):
    """GitHub rejects a matrix built from an empty list, so "none" must exist."""
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    emit_github_output([])
    assert out.read_text().strip() == 'tasks=["none"]'


def test_github_output_is_silent_outside_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    emit_github_output(["start:abc"])  # must not raise


# ---- missed --------------------------------------------------------------


class FakeDb:
    """Records calls instead of talking to Supabase."""

    def __init__(self, pending):
        self.pending = pending
        self.updates = []
        self.events = []

    def select(self, table, **params):
        return self.pending if table == "jobs" else []

    def update(self, table, match, patch):
        self.updates.append((match, patch))
        return []

    def insert(self, table, rows):
        # Record the table too — otherwise an assertion like "was a job_events
        # row written?" cannot be expressed, since the table name is the only
        # thing that says which table the row went to.
        self.events.append((table, rows))
        return []


def test_pending_job_past_grace_is_marked_missed():
    stale = {"id": "j1", "planned_start_at": (MONDAY - timedelta(hours=2)).isoformat()}
    db = FakeDb([stale])
    mark_missed(db, MONDAY, grace=30)
    assert db.updates[0][1]["state"] == "missed"
    assert db.updates[0][1]["error_code"] == "MISSED_WINDOW"


def test_pending_job_inside_grace_is_left_alone():
    fresh = {"id": "j2", "planned_start_at": (MONDAY - timedelta(minutes=5)).isoformat()}
    db = FakeDb([fresh])
    mark_missed(db, MONDAY, grace=30)
    assert db.updates == []


def test_a_missed_job_always_records_an_event():
    """A silently-skipped schedule would look like the app is broken."""
    stale = {"id": "j1", "planned_start_at": (MONDAY - timedelta(hours=2)).isoformat()}
    db = FakeDb([stale])
    mark_missed(db, MONDAY, grace=30)
    table, row = db.events[0]
    assert table == "job_events"
    assert row["job_id"] == "j1"
    assert len(db.events) == 1