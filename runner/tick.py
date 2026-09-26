"""tick — decide which schedules are due, and record the jobs they imply.

Runs on a `*/5` cron in GitHub Actions. Its whole job is the *decision*: turn
schedules into at-most-once job rows. It never talks to Kaggle. That split is
deliberate — a decision this cheap can be retried and re-run harmlessly, while
starting a Kaggle run cannot be undone.

Idempotency rests on one database constraint: `unique (schedule_id,
planned_start_at)`. Two overlapping ticks both compute the same job; the loser's
insert is ignored and nothing is started twice.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone

from .schedule_engine import ScheduleSpec, is_past_grace, materialize
from .supabase_client import SupabaseClient

DEFAULT_HORIZON_MINUTES = 10
DEFAULT_GRACE_MINUTES = 30


def _log(msg: str) -> None:
    print(f"[tick] {msg}", flush=True)


def load_settings(db: SupabaseClient) -> dict:
    """Key/value settings, with defaults applied when a key is absent."""
    rows = db.select("settings", select="key,value") or []
    flat = {row["key"]: row["value"] for row in rows}
    return {
        "horizon_minutes": int(flat.get("horizon_minutes", DEFAULT_HORIZON_MINUTES)),
        "grace_minutes": int(flat.get("grace_minutes", DEFAULT_GRACE_MINUTES)),
    }


def load_schedules(db: SupabaseClient) -> list[dict]:
    """Active schedules, with the notebook fields the engine needs.

    Timezone comes from the *schedule* (not the notebook) because a user may
    legitimately want one notebook scheduled in two zones.
    """
    return db.select(
        "schedules",
        select=(
            "id,notebook_id,days_of_week,start_time,duration_minutes,stop_at_time,"
            "timezone,start_date,end_date,is_active,"
            "notebooks(id,kaggle_ref,account_id,is_active)"
        ),
        is_active="eq.true",
        order="id",
    ) or []


def to_spec(row: dict) -> ScheduleSpec:
    """Build the engine's input from a database row.

    Postgres `time`/`date` arrive as strings over PostgREST, so they are parsed
    here — the engine itself only ever sees real `time`/`date` objects.
    """
    stop_at = row.get("stop_at_time")
    return ScheduleSpec(
        id=str(row["id"]),
        days_of_week=tuple(sorted(int(d) for d in row["days_of_week"])),
        start_time=time.fromisoformat(str(row["start_time"])),
        duration_minutes=int(row["duration_minutes"]),
        stop_at_time=time.fromisoformat(str(stop_at)) if stop_at else None,
        timezone=row.get("timezone") or "Asia/Dhaka",
        start_date=date.fromisoformat(str(row["start_date"])) if row.get("start_date") else None,
        end_date=date.fromisoformat(str(row["end_date"])) if row.get("end_date") else None,
    )


def due_job_rows(schedules: list[dict], now_utc: datetime, horizon: int) -> list[dict]:
    rows: list[dict] = []
    for row in schedules:
        notebook = row.get("notebooks") or {}
        # A schedule whose notebook was deactivated or deleted is skipped
        # rather than failing the whole tick.
        if not notebook or not notebook.get("is_active", True):
            _log(f"skipping schedule {row['id']}: notebook inactive or missing")
            continue

        try:
            spec = to_spec(row)
        except (KeyError, ValueError) as exc:
            _log(f"skipping malformed schedule {row['id']}: {exc}")
            continue

        for job in materialize(spec, now_utc, horizon_minutes=horizon):
            rows.append(
                {
                    "schedule_id": job.schedule_id,
                    "notebook_id": notebook["id"],
                    "account_id": notebook["account_id"],
                    "state": "pending",
                    "planned_start_at": job.planned_start_at.isoformat(),
                    "planned_stop_at": job.planned_stop_at.isoformat(),
                    "stop_reason_pref": job.stop_reason_pref,
                    "timeout_seconds": job.timeout_seconds,
                }
            )
    return rows


def mark_missed(db: SupabaseClient, now_utc: datetime, grace: int) -> int:
    """Flag pending jobs whose window closed while nothing was running.

    Being explicit about this matters: a job that never started because GitHub's
    scheduler was down should be visible as `missed`, not sit in `pending`
    forever looking like it is about to run.
    """
    cutoff = now_utc - timedelta(minutes=grace)
    pending = db.select(
        "jobs",
        select="id,planned_start_at",
        state="eq.pending",
        planned_start_at=f"lt.{cutoff.isoformat()}",
    ) or []
    for job in pending:
        start = datetime.fromisoformat(str(job["planned_start_at"]).replace("Z", "+00:00"))
        if is_past_grace(start, now_utc, grace):
            db.update("jobs", {"id": job["id"]}, {"state": "missed", "error_code": "MISSED_WINDOW"})
            db.insert(
                "job_events",
                {
                    "job_id": job["id"],
                    "level": "warn",
                    "message": "window closed before the runner could start it",
                },
            )
    return len(pending)


def emit_github_output(tasks: list[str]) -> None:
    """Publish one ``mode:job_id`` entry per created job, for the workflow matrix.

    The mode is included so each matrix leg is self-describing — the workflow
    forwards the string straight to the worker. A value is always emitted,
    because GitHub rejects a matrix built from an empty list, so the no-work
    case sends the sentinel ``none`` and the downstream job skips itself.
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    payload = json.dumps(tasks or ["none"])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"tasks={payload}\n")


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    _log(f"tick at {now_utc.isoformat()}")

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        _log("SUPABASE_URL / SUPABASE_SECRET_KEY not set")
        return 2

    db = SupabaseClient(url, key)
    settings = load_settings(db)
    _log(f"settings: {settings}")

    schedules = load_schedules(db)
    _log(f"{len(schedules)} active schedule(s)")

    rows = due_job_rows(schedules, now_utc, settings["horizon_minutes"])
    _log(f"{len(rows)} job(s) due")

    created = db.insert_ignore_duplicates("jobs", rows) if rows else []
    created = created or []
    _log(f"{len(created)} new job(s) created; {len(rows) - len(created)} already existed")

    for job in created:
        db.insert(
            "job_events",
            {
                "job_id": job["id"],
                "level": "info",
                "message": (
                    f"scheduled: start {job['planned_start_at']} stop {job['planned_stop_at']} "
                    f"({job.get('stop_reason_pref', 'duration')})"
                ),
                "payload": json.dumps({"timeout_seconds": job.get("timeout_seconds")}),
            },
        )

    missed = mark_missed(db, now_utc, settings["grace_minutes"])
    if missed:
        _log(f"{missed} pending job(s) past grace — failing them as missed")

    emit_github_output([f"start:{job['id']}" for job in created])
    return 0


if __name__ == "__main__":
    sys.exit(main())