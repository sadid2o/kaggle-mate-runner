"""Phase 0 — feasibility checks, run as one GitHub Actions job.

Nothing here is app code. The point is to answer the three questions the whole
design rests on, with real output, before a single line of the app is built:

  Q1  Does `kaggle kernels push` trigger a run at all?
  Q2  Does `--timeout` stop it at the deadline, and what status does that leave?
  Q3  Does an injected watchdog stop it, and does Kaggle call that an error?

Every check prints its evidence. Where a result is inconclusive the script says
so instead of guessing — a wrong "yes" here would be expensive later.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .kaggle_cli import TERMINAL, KaggleCli, KaggleCredentials, KaggleError, classify_status
from .push_builder import build_push_folder, parse_metadata

OUT = Path(".phase0")
POLL_SECONDS = 20


def _log(msg: str) -> None:
    print(f"[phase0] {msg}", flush=True)


def _banner(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}", flush=True)


def _dummy_notebook() -> dict:
    """A notebook that logs a heartbeat every 10s.

    Chosen over a real notebook because it makes *timing* observable: the log
    shows the exact last heartbeat before the run died, which is how we tell a
    deadline stop from a crash.
    """
    source = (
        "import time, datetime\n"
        "for i in range(10000):\n"
        "    print(f'heartbeat {i} at {datetime.datetime.now(datetime.timezone.utc).isoformat()}', flush=True)\n"
        "    time.sleep(10)\n"
    )
    return {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": source.splitlines(keepends=True),
            }
        ],
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _write_dummy(dirpath: Path, slug: str, username: str) -> tuple[str, bytes]:
    dirpath.mkdir(parents=True, exist_ok=True)
    code_file = "phase0-heartbeat.ipynb"
    (dirpath / code_file).write_text(json.dumps(_dummy_notebook()), encoding="utf-8")

    meta = {
        "id": f"{username}/{slug}",
        "title": "kaggle-mate phase0 heartbeat",
        "code_file": code_file,
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": False,
        "enable_internet": False,
        "dataset_sources": [],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }
    (dirpath / "kernel-metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return f"{username}/{slug}", (dirpath / code_file).read_bytes()


def _poll(cli: KaggleCli, ref: str, budget_seconds: int) -> list[tuple[float, str]]:
    """Poll status until the run leaves the running/queued set or budget ends."""
    samples: list[tuple[float, str]] = []
    started = time.time()
    while time.time() - started < budget_seconds:
        try:
            raw = cli.status(ref)
        except KaggleError as exc:
            raw = f"ERROR: {exc.stderr or exc}"
        state = classify_status(raw)
        samples.append((time.time() - started, raw))
        _log(f"  t+{int(time.time() - started)}s  {raw}   -> {state}")
        if state in TERMINAL:
            break
        time.sleep(POLL_SECONDS)
    return samples


def q1_push(cli: KaggleCli, ref: str, folder: Path) -> bool:
    _banner("Q1 — does `kaggle kernels push` trigger a run?")
    try:
        out = cli.push(folder)
        _log(f"push returned: {out or '(no output)'}")
    except KaggleError as exc:
        _log(f"PUSH FAILED: {exc}\n{exc.stderr}")
        return False

    samples = _poll(cli, ref, budget_seconds=300)
    states = {classify_status(raw) for _, raw in samples}
    ok = bool(states & {"queued", "running", "completed"})
    unrecognised = [s for s in states if s.startswith("unknown(")]

    if ok:
        _log("Q1 verdict: YES — push starts a run")
    elif unrecognised and len(unrecognised) == len(states):
        # The literal status text is undocumented — the CLI reference only
        # describes it in words ("queued, running, complete, or errored").
        # If every sample came back unrecognised, the *classifier* is what is
        # wrong, not the API, so say so instead of reporting a false NO.
        _log("Q1 verdict: INCONCLUSIVE — not a failure.")
        _log("   `kernels status` returned text the classifier does not recognise.")
        _log("   Copy the literal value printed above into runner/kaggle_cli.py and")
        _log("   re-run, rather than assuming a run did or did not start.")
    else:
        _log("Q1 verdict: NO — no run observed")

    _log(f"   states seen: {sorted(states)}")
    return ok


def q2_timeout(cli: KaggleCli, ref: str, folder: Path, seconds: int) -> dict:
    _banner(f"Q2 — does --timeout {seconds}s stop the run at the deadline?")
    meta = parse_metadata((folder / "kernel-metadata.json").read_bytes())
    code_file = meta["code_file"]

    plan = build_push_folder(
        dest=folder,
        code_bytes=(folder / code_file).read_bytes(),
        code_file=code_file,
        metadata=meta,
        deadline_utc=datetime.now(timezone.utc) + timedelta(days=365),  # watchdog effectively off
    )
    _log(f"watchdog injected for Q2: {plan.watchdog_injected} (should be False — isolating the timeout)")

    started = datetime.now(timezone.utc)
    try:
        cli.push(folder, timeout_seconds=seconds)
    except KaggleError as exc:
        _log(f"PUSH FAILED: {exc.stderr}")
        return {"ok": False, "reason": "push failed"}

    samples = _poll(cli, ref, budget_seconds=seconds + 420)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    verdict = {
        "ok": elapsed < seconds + 360,
        "elapsed_seconds": elapsed,
        "final_raw": samples[-1][1] if samples else "",
        "final_state": classify_status(samples[-1][1]) if samples else "none",
    }
    _log(f"Q2 verdict: ran {int(elapsed)}s (limit {seconds}s)"
         f" — {'stopped near deadline' if verdict['ok'] else 'ran long / inconclusive'}")
    _log(f"   final status: {verdict['final_state']}  raw={verdict['final_raw']}")
    return verdict


def q3_watchdog(cli: KaggleCli, ref: str, folder: Path, seconds: int) -> dict:
    _banner(f"Q3 — does the injected watchdog stop the run, and is it an error?")
    meta = parse_metadata((folder / "kernel-metadata.json").read_bytes())
    code_file = meta["code_file"]

    deadline = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    plan = build_push_folder(
        dest=folder,
        code_bytes=(folder / code_file).read_bytes(),
        code_file=code_file,
        metadata=meta,
        deadline_utc=deadline,  # timeout deliberately far off, watchdog is the only stop
    )
    _log(f"deadline set at {deadline.isoformat()} (t+{seconds}s), watchdog injected={plan.watchdog_injected}")
    if not plan.watchdog_injected:
        _log("watchdog NOT injected — Q3 cannot be answered with this file type")
        return {"ok": False, "reason": "injection unsupported for this language"}

    started = datetime.now(timezone.utc)
    try:
        cli.push(folder, timeout_seconds=seconds * 4)
    except KaggleError as exc:
        _log(f"PUSH FAILED: {exc.stderr}")
        return {"ok": False, "reason": "push failed"}

    samples = _poll(cli, ref, budget_seconds=seconds + 420)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    final_state = classify_status(samples[-1][1]) if samples else "none"

    # The log tells us *where* it stopped, which the status alone cannot.
    log_text = ""
    try:
        log_text = cli.logs(ref)
    except KaggleError as exc:
        _log(f"log fetch failed: {exc.stderr}")

    verdict = {
        "ok": elapsed < seconds + 300,
        "elapsed_seconds": elapsed,
        "final_state": final_state,
        "deadline_marker": "deadline reached" in log_text,
        "heartbeats": log_text.count("heartbeat"),
    }
    _log(f"Q3 verdict: ran {int(elapsed)}s against a t+{seconds}s deadline")
    _log(f"   watchdog message in log: {verdict['deadline_marker']}")
    _log(f"   final status: {final_state}  (if 'failed', Kaggle treats this as an error)")
    return verdict


def main() -> int:
    _banner("Kaggle Mate — Phase 0 feasibility")
    raw_secret = os.environ.get("KAGGLE_TOKEN", "").strip()
    if not raw_secret:
        _log("KAGGLE_TOKEN is not set. Add it as a GitHub secret (username:key or kaggle.json blob).")
        return 2

    creds = KaggleCredentials.from_secret(raw_secret)
    username = creds.username
    cli = KaggleCli(creds)
    _log(f"account: {username}")

    _banner("0.0 — token works at all")
    try:
        _log(f"kernels list OK: {cli.verify()[:200] or '(empty list — no kernels yet)'}")
    except KaggleError as exc:
        _log(f"AUTH FAILED: {exc.stderr}")
        return 2

    slug = f"kaggle-mate-phase0-{datetime.now(timezone.utc).strftime('%m%d%H%M')}"
    folder = OUT / "kernel"
    ref, _ = _write_dummy(folder, slug, username)
    _log(f"generated test kernel: {ref} (private)")

    results: dict[str, object] = {}
    results["q1"] = q1_push(cli, ref, folder)
    if not results["q1"]:
        _log("Q1 failed — remaining checks are meaningless. Stopping.")
    else:
        results["q2"] = q2_timeout(cli, ref, folder, seconds=420)
        results["q3"] = q3_watchdog(cli, ref, folder, seconds=300)

    _banner("SUMMARY")
    print(json.dumps(results, indent=2, default=str), flush=True)

    OUT.mkdir(exist_ok=True)
    (OUT / "phase0-results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    _log("wrote .phase0/phase0-results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())