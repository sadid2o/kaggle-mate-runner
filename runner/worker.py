"""worker — claim one job and actually run it on Kaggle.

Three modes, all entering through the same claiming RPC so two concurrent
worker runs can never run the same job twice:

  start    fetch the notebook source, build a push copy with the deadline
           watchdog, push it, then poll until it ends. Reaching the deadline is
           a *success*, not a failure.
  cancel   the user pressed cancel. Best effort — see below.
  reap     the scheduled-started run died before it could stop itself (runner
           out of minutes, Kaggle queue, crash). Catch it after the fact.

On cancel, be honest about the limit: Kaggle exposes no documented
"stop this kernel" endpoint. Phase 0 check Q8 probes whether one exists. Until
that answer is in, cancel is *cooperative* — the watchdog cell reads a flag from
Supabase and exits at its next 30-second check, so a cancel lands within about
30 seconds rather than instantly.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .kaggle_cli import TERMINAL, KaggleCli, KaggleCredentials, KaggleError, classify_status
from .push_builder import build_push_folder, parse_metadata
from .supabase_client import SupabaseClient
from .watchdog import CancelChannel

POLL_SECONDS = 30
#: How long the worker watches a run before handing over to `reap`. Kept under
#: GitHub's 6-hour job ceiling with room to finish the final poll.
MAX_WATCH_SECONDS = 5 * 3600 + 1800


def _log(msg: str) -> None:
    print(f"[worker] {msg}", flush=True)


def _event(db: SupabaseClient, job_id: str, message: str, level: str = "info", **extra) -> None:
    db.insert(
        "job_events",
        {"job_id": job_id, "level": level, "message": message, **extra},
    )


#: States a job can rest in. `watching` is deliberately absent: it means "a
#: worker has stepped back but the run is still going", so it must not get a
#: finished_at — otherwise the dashboard would show a live run as ended.
FINAL_STATES = {"stopped", "completed", "failed", "cancelled", "missed"}


def _finish(db: SupabaseClient, job_id: str, state: str, **patch) -> None:
    body = {"state": state, **patch}
    if state in FINAL_STATES:
        body["finished_at"] = datetime.now(timezone.utc).isoformat()
    db.update("jobs", {"id": job_id}, body)


# ---- credentials ---------------------------------------------------------


def account_credentials(db: SupabaseClient, account_id: str) -> KaggleCredentials:
    """Read one account's token through the Vault RPC.

    The secret never appears in a SELECT result: `vault.decrypted_secrets` is
    reachable only through a `security definer` function, so the service key can
    fetch exactly one account's token at a time and nothing else.
    """
    result = db.rpc("km_get_account_secret", {"p_account_id": account_id})
    if not result:
        raise RuntimeError(f"account {account_id} has no stored credential")
    row = result[0] if isinstance(result, list) else result
    return KaggleCredentials.from_secret(row["secret"])


# ---- start ---------------------------------------------------------------


def _cancel_channel(db: SupabaseClient, job: dict) -> CancelChannel | None:
    """Build the notebook-side cancel poll, or ``None`` when it cannot work.

    Returns ``None`` rather than raising when the publishable key is not
    configured: the deadline watchdog is independent of this, so a missing
    publishable key must degrade "Cancel now" from instant to "waits for the
    deadline" — not break the run that is about to start.
    """
    publishable = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if not publishable:
        return None
    if publishable.startswith("sb_secret_"):
        # Wrong key in the wrong variable. The notebook is public code; the
        # secret key would hand over the entire project.
        raise RuntimeError(
            "SUPABASE_PUBLISHABLE_KEY holds a secret key; refusing to embed it in a notebook"
        )
    return CancelChannel(
        url=f"{db.url}/rest/v1/rpc/km_cancel_poll",
        api_key=publishable,
        job_id=str(job["id"]),
    )


def start(db: SupabaseClient, job: dict) -> int:
    """Claim one job and run it on Kaggle.

    Two stop mechanisms are armed at push time, and the difference between them
    matters to the user:

    * the **deadline**, baked into the notebook as a watchdog thread. It serves
      both scheduled stop rules — "after N hours" and "at a clock time" — and
      works with the internet switched off.
    * the **cancel channel**, which the watchdog polls so an ad-hoc "Cancel now"
      lands within ~30s. This one needs internet and the publishable key, so it
      is armed only when it can actually work.
    """
    job_id = str(job["id"])
    notebook = db.select("notebooks", select="*", id=f"eq.{job['notebook_id']}")[0]
    version = db.select(
        "notebook_versions", select="*", notebook_id=f"eq.{job['notebook_id']}", order="version.desc", limit=1
    )[0]

    _log(f"job {job_id}: {notebook['kaggle_ref']} v{version['version']}")
    _event(db, job_id, f"picked up: {notebook['kaggle_ref']} v{version['version']}")

    try:
        creds = account_credentials(db, str(job["account_id"]))
    except Exception as exc:  # noqa: BLE001 - any failure here is AUTH_FAILED
        _event(db, job_id, f"credential lookup failed: {exc}", level="error")
        _finish(db, job_id, "failed", error_code="AUTH_FAILED")
        return 0

    cli = KaggleCli(creds)

    # ---- source ----------------------------------------------------------
    code = db.storage_download("notebooks", version["code_path"])
    meta = parse_metadata(db.storage_download("notebooks", version["meta_path"]))
    code_file = meta.get("code_file") or Path(version["code_path"]).name

    deadline = datetime.fromisoformat(str(job["planned_stop_at"]).replace("Z", "+00:00"))

    # The cancel channel needs the notebook to reach Supabase, so it is only
    # meaningful when the run has internet (and only for Python, which is the
    # only language the watchdog can be injected into). Without internet the
    # deadline still fires — the user's scheduled stop rules are unaffected —
    # but an early "Cancel now" cannot reach the run, and the job record says so
    # rather than leaving the user wondering why the button did nothing.
    cancel = _cancel_channel(db, job)
    if cancel is None and meta.get("enable_internet") is not True:
        _event(
            db,
            job_id,
            "run has no internet: the deadline is enforced, but 'Cancel now' cannot reach it",
            level="warn",
        )

    with tempfile.TemporaryDirectory(prefix="km-push-") as tmp:
        plan = build_push_folder(
            dest=Path(tmp),
            code_bytes=code,
            code_file=code_file,
            metadata=meta,
            deadline_utc=deadline,
            cancel=cancel,
        )
        _log(f"push folder ready: {plan.code_file} (watchdog={plan.watchdog_injected})")
        if not plan.watchdog_injected:
            _event(
                db,
                job_id,
                f"{plan.language} run: no deadline watchdog (only Kaggle's own timeout applies)",
                level="warn",
            )

        try:
            cli.push(Path(tmp), timeout_seconds=int(job["timeout_seconds"]))
        except KaggleError as exc:
            code_tag = "QUOTA_LIMIT" if _looks_like_quota(exc.stderr) else "PUSH_FAILED"
            _event(db, job_id, f"push failed: {exc.stderr[:1000]}", level="error")
            _finish(db, job_id, "failed", error_code=code_tag)
            return 0

    db.update("jobs", {"id": job_id}, {"state": "running", "started_at": datetime.now(timezone.utc).isoformat()})
    _event(
        db,
        job_id,
        "push accepted by Kaggle; polling for status"
        + ("" if cancel else " (cancel channel not armed — see earlier event)"),
    )

    # ---- poll ------------------------------------------------------------
    ref = notebook["kaggle_ref"]
    began = time.time()
    last_state = "unknown"
    while time.time() - began < MAX_WATCH_SECONDS:
        try:
            raw = cli.status(ref)
            last_state = classify_status(raw)
        except KaggleError as exc:
            _log(f"status check failed (will retry): {exc.stderr[:200]}")
            last_state = "unknown"

        if last_state != "unknown":
            db.update("jobs", {"id": job_id}, {"last_status": last_state})

        if last_state in TERMINAL:
            break
        time.sleep(POLL_SECONDS)

    # ---- outcome ---------------------------------------------------------
    if last_state == "cancelled":
        _event(db, job_id, "run ended after a cancel request")
        _finish(db, job_id, "cancelled", stop_reason="cancelled_by_user")
        _store_log(db, cli, job_id, ref)
        return 0

    past_deadline = datetime.now(timezone.utc) >= deadline
    if last_state in {"completed", "failed"} and past_deadline:
        # os._exit at the deadline makes Kaggle report `error`; that is the
        # expected signature of a deadline stop, not a real failure.
        _event(db, job_id, f"stopped at deadline (kaggle reports: {last_state})")
        _finish(db, job_id, "stopped", stop_reason=job.get("stop_reason_pref") or "duration")
        _store_log(db, cli, job_id, ref)
        return 0

    if last_state == "failed":
        # An abrupt exit makes Kaggle report `error` whether the watchdog fired
        # on the deadline or on a cancel request, and both are the *intended*
        # stop rather than a crash. The deadline case is handled above; this
        # covers the cancel arriving early, so ask which one actually happened
        # instead of assuming the worst and alarming the user.
        if _cancel_was_requested(db, job_id):
            _event(db, job_id, "stopped early: cancel was requested and the watchdog exited")
            _finish(db, job_id, "cancelled", stop_reason="cancelled_by_user")
        else:
            _event(db, job_id, "Kaggle reported a run error before the deadline", level="error")
            _finish(db, job_id, "failed", error_code="KAGGLE_RUN_ERROR")
        _store_log(db, cli, job_id, ref)
        return 0

    if last_state == "completed":
        _event(db, job_id, "notebook finished on its own before the deadline")
        _finish(db, job_id, "completed", stop_reason="notebook_finished")
        _store_log(db, cli, job_id, ref)
        return 0

    # Out of watch budget: hand over to `reap`, which runs later.
    _event(db, job_id, "worker watch window ended; handing over to reap", level="warn")
    _finish(db, job_id, "watching")
    return 0


def _looks_like_quota(stderr: str) -> bool:
    text = stderr.lower()
    return any(token in text for token in ("quota", "limit", "too many", "exceeded"))


def _cancel_was_requested(db: SupabaseClient, job_id: str) -> bool:
    """Did the user press Cancel while this job was running?

    Read back from the database rather than inferred from the stop time. A
    cancel that lands two minutes before the deadline and a deadline stop that
    happens to fire from a late tick are indistinguishable by timing alone, and
    guessing between "you cancelled this" and "it failed" is exactly the kind of
    wrong answer that makes a user distrust the whole app. The flag is
    authoritative, so it is what gets asked.
    """
    try:
        rows = db.select("jobs", select="cancel_requested", id=f"eq.{job_id}")
    except Exception:  # noqa: BLE001 - must never change the outcome
        return False
    return bool(rows and rows[0].get("cancel_requested"))


def _store_log(db: SupabaseClient, cli: KaggleCli, job_id: str, ref: str) -> None:
    """Store the run's log text.

    Deliberately **not** the full output directory. The free Supabase tier
    allows 1 GB of storage in total, and a real notebook's artifacts can be
    gigabytes on their own — pulling them in would fill the project and break
    everything else. The log is the part that answers "what did this run do?";
    the rest stays on Kaggle, where the app can link to it.

    A failure here is logged but never changes the job's outcome — the run
    already succeeded or failed, and losing the log must not rewrite that.
    """
    try:
        log_text = cli.logs(ref)
    except Exception as exc:  # noqa: BLE001
        _event(db, job_id, f"OUTPUT_FETCH_FAILED: {exc}", level="warn", error_code="OUTPUT_FETCH_FAILED")
        return

    if not log_text:
        _event(db, job_id, "no log text returned by Kaggle", level="warn")
        return

    try:
        db.storage_upload(
            "jobs",
            f"{job_id}/log.txt",
            log_text.encode("utf-8"),
            "text/plain; charset=utf-8",
        )
        db.update("jobs", {"id": job_id}, {"log_path": f"{job_id}/log.txt"})
        _event(db, job_id, f"log stored ({len(log_text)} chars)")
    except Exception as exc:  # noqa: BLE001
        _event(db, job_id, f"OUTPUT_FETCH_FAILED: {exc}", level="warn", error_code="OUTPUT_FETCH_FAILED")


# ---- cancel --------------------------------------------------------------


def cancel(db: SupabaseClient, job: dict) -> int:
    """Ask a running job to stop, cooperatively.

    Sets the flag the watchdog cell is watching; the run then exits on its own
    within ~30s. A job that never started is finalised by the claim RPC itself,
    so this returns immediately for that case.
    """
    job_id = str(job["id"])

    # The claim RPC is what decided this, atomically: it finalises a job that
    # had not started yet, and leaves a running one alone with the flag set.
    # So a state of 'cancelled' here means the work is already done.
    if job.get("state") == "cancelled":
        _event(db, job_id, "cancelled before it started — nothing was running")
        return 0

    _event(
        db,
        job_id,
        "cancel requested: the notebook's watchdog polls this flag every 30s and exits",
        level="warn",
    )
    _log("cancel flag set (cooperative stop; see module docstring for why)")

    ref = db.select("notebooks", select="kaggle_ref", id=f"eq.{job['notebook_id']}")[0]["kaggle_ref"]
    creds = account_credentials(db, str(job["account_id"]))
    cli = KaggleCli(creds)

    began = time.time()
    while time.time() - began < 300:  # 5 min is plenty for a 30s poll loop
        try:
            state_now = classify_status(cli.status(ref))
        except KaggleError:
            state_now = "unknown"

        if state_now in TERMINAL:
            _finish(
                db,
                job_id,
                "cancelled",
                stop_reason="cancelled_by_user",
                error_code="CANCELLED_BY_USER",
            )
            _event(db, job_id, "run has ended after the cancel request")
            return 0

        # The job may have been reaped or finished by another leg while we
        # waited. Stopping here avoids declaring a timeout on a job that is
        # already resolved.
        if not _still_open(db, job_id):
            _log("job already finalised elsewhere; stopping the cancel watch")
            return 0

        time.sleep(20)

    # Not a failure of the run. Either the notebook had no internet to hear the
    # poll, or it is wedged. Both are reported as what they are, and the job is
    # left in `running` so the deadline watchdog and `reap` still resolve it —
    # marking it `failed` here would blame the job for the user's own cancel.
    _event(
        db,
        job_id,
        "cancel request could not stop the run within 5 minutes — it may have no "
        "internet, or the notebook is not responding. The deadline stop still applies.",
        level="warn",
        error_code="CANCEL_UNCONFIRMED",
    )
    return 0


def _still_open(db: SupabaseClient, job_id: str) -> bool:
    """True while the job has not reached a final state."""
    try:
        rows = db.select("jobs", select="state", id=f"eq.{job_id}")
    except Exception:  # noqa: BLE001
        return True  # unknown: keep waiting rather than abandon the cancel
    if not rows:
        return False
    return rows[0].get("state") not in FINAL_STATES


# ---- reap ----------------------------------------------------------------


def reap(db: SupabaseClient, job: dict) -> int:
    """Finalise a job whose runner stopped watching before the job ended."""
    job_id = str(job["id"])
    ref = db.select("notebooks", select="kaggle_ref,account_id", id=f"eq.{job['notebook_id']}")[0]
    creds = account_credentials(db, str(job.get("account_id") or ref["account_id"]))
    cli = KaggleCli(creds)

    try:
        state = classify_status(cli.status(ref["kaggle_ref"]))
    except KaggleError as exc:
        _event(db, job_id, f"reap status check failed: {exc.stderr[:300]}", level="warn")
        state = "unknown"

    deadline = datetime.fromisoformat(str(job["planned_stop_at"]).replace("Z", "+00:00"))
    if state in {"completed", "failed"}:
        outcome = "stopped" if datetime.now(timezone.utc) >= deadline else (
            "failed" if state == "failed" else "completed"
        )
        _finish(db, job_id, outcome, stop_reason=job.get("stop_reason_pref"))
        _event(db, job_id, f"reaped: final kaggle status {state} -> {outcome}")
    else:
        # Still running past its deadline with nobody watching is exactly the
        # case the two-layer stop exists to prevent; record it loudly.
        _event(db, job_id, f"still {state} after its deadline with no watcher", level="error")
        _finish(db, job_id, "failed", error_code="DEADLINE_STOP_UNCONFIRMED")
    return 0


# ---- entry ---------------------------------------------------------------


MODES = {
    "start": ("km_claim_job", start),
    "cancel": ("km_claim_job", cancel),
    "reap": ("km_claim_job", reap),
}


def parse_task(arg: str) -> tuple[str, str | None]:
    """Accept ``mode``, ``mode:job_id`` or ``mode --job-id ID``.

    The combined form exists because the tick workflow builds its matrix
    straight from these strings, so each leg's argument has to be one
    self-contained token.
    """
    arg = arg.strip()
    job_id = None
    if "--job-id" in arg:
        parts = arg.split()
        arg = parts[0]
        job_id = parts[parts.index("--job-id") + 1]
    if ":" in arg:
        mode, _, rest = arg.partition(":")
        return mode.strip(), rest.strip() or job_id
    return arg, job_id


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"usage: python -m runner.worker [{'|'.join(MODES)}][:JOB_ID]", file=sys.stderr)
        return 2

    mode, job_id = parse_task(" ".join(argv[1:]))
    if mode not in MODES:
        print(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}", file=sys.stderr)
        return 2

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
    if not url or not key:
        _log("SUPABASE_URL / SUPABASE_SECRET_KEY not set")
        return 2

    db = SupabaseClient(url, key)
    rpc, handler = MODES[mode]

    # `km_claim_job` locks the row and flips it to the mode's working state in
    # one statement, so an overlapping worker gets nothing rather than a second
    # copy of the same job.
    job = db.rpc(rpc, {"p_mode": mode, "p_job_id": job_id})
    job = job[0] if isinstance(job, list) and job else (job if isinstance(job, dict) else None)

    if not job:
        _log(f"nothing to {mode}")
        return 0

    _log(f"{mode} job {job['id']}")
    try:
        return handler(db, job)
    except Exception as exc:  # noqa: BLE001 - a crashed worker must still record why
        _log(f"unhandled error: {exc!r}")
        _event(db, str(job["id"]), f"worker crashed: {exc!r}", level="error")
        _finish(db, str(job["id"]), "failed", error_code="WORKER_CRASH")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))