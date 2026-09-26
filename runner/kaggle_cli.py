"""Thin wrapper over the Kaggle CLI.

Deliberately shells out to the documented ``kaggle`` command rather than
calling undocumented Python methods: every invocation below maps to a command
listed in Kaggle's own CLI reference, so nothing here is guesswork.

Credentials are passed per call through an isolated temp config dir, never
through module state — which is what makes several accounts usable inside one
process without them leaking into each other.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

TIMEOUT_DEFAULT = 300


class KaggleError(RuntimeError):
    """Raised when the CLI exits non-zero. Carries stderr for the alert body."""

    def __init__(self, message: str, stderr: str = "", code: int | None = None):
        super().__init__(message)
        self.stderr = stderr
        self.code = code


@dataclass(frozen=True)
class KaggleCredentials:
    """One account's credential.

    Two shapes are accepted, because Kaggle currently supports two and they are
    not interchangeable:

    **Legacy** (`username` set) — the `kaggle.json` download from
    *Create Legacy API Key*. Needs a config directory, so it goes through a temp
    `KAGGLE_CONFIG_DIR`.

    **Token** (`username` is ``None``) — a bare token, passed as
    `KAGGLE_API_TOKEN`. Simpler: no file, no temp directory.

    Kaggle's docs now list `kaggle auth login` (OAuth) and `KAGGLE_API_TOKEN`
    ahead of the legacy file, and label the file "legacy" — so the token path is
    the one that will survive. Both are kept working here rather than betting on
    one. Source: https://github.com/Kaggle/kaggle-cli/blob/main/docs/README.md
    (checked 2026-09-26).
    """

    key: str
    username: str | None = None

    @classmethod
    def from_secret(cls, raw: str) -> "KaggleCredentials":
        """Accept ``username:key``, a ``kaggle.json`` blob, or a bare token."""
        raw = raw.strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            return cls(key=data["key"], username=data.get("username"))
        if ":" in raw:
            username, _, key = raw.partition(":")
            return cls(key=key.strip(), username=username.strip() or None)
        # No username and no JSON: treat as a modern API token.
        return cls(key=raw, username=None)


@contextmanager
def credentials_env(creds: KaggleCredentials) -> Iterator[dict[str, str]]:
    """Yield an env carrying exactly this account's credential, and no other.

    Isolation is the whole point: several accounts run in one process, and a
    stale credential leaking between them would push someone else's notebook to
    the wrong account. Whatever the previous call left behind is cleared first,
    and any temp directory is removed on exit.
    """
    env = dict(os.environ)
    # Clear every variable the CLI reads, so nothing from the outer environment
    # (or a previous account) can shadow what we set below.
    for var in ("KAGGLE_CONFIG_DIR", "KAGGLE_USERNAME", "KAGGLE_KEY", "KAGGLE_API_TOKEN"):
        env.pop(var, None)

    if creds.username is None:
        env["KAGGLE_API_TOKEN"] = creds.key
        yield env
        return

    tmp = Path(tempfile.mkdtemp(prefix="km-kaggle-"))
    try:
        cfg = tmp / "kaggle.json"
        cfg.write_text(
            json.dumps({"username": creds.username, "key": creds.key}),
            encoding="utf-8",
        )
        try:
            os.chmod(cfg, 0o600)  # best effort; a no-op on Windows
        except OSError:
            pass
        env["KAGGLE_CONFIG_DIR"] = str(tmp)
        yield env
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(args: list[str], env: dict[str, str], timeout: int = TIMEOUT_DEFAULT) -> str:
    exe = shutil.which("kaggle") or "kaggle"
    proc = subprocess.run(
        [exe, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise KaggleError(
            f"kaggle {' '.join(args)} failed",
            stderr=(proc.stderr or proc.stdout or "").strip(),
            code=proc.returncode,
        )
    return (proc.stdout or "").strip()


class KaggleCli:
    """Every operation the runner needs, for one account at a time."""

    def __init__(self, creds: KaggleCredentials):
        self.creds = creds

    # ---- account -------------------------------------------------------

    def verify(self) -> str:
        """Cheapest authenticated call — proves the token actually works."""
        with credentials_env(self.creds) as env:
            return _run(["kernels", "list", "-m", "--page-size", "1"], env)

    # ---- read ----------------------------------------------------------

    def status(self, ref: str) -> str:
        """Raw status line. Kaggle returns e.g. 'KernelWorkerStatus.RUNNING'."""
        with credentials_env(self.creds) as env:
            return _run(["kernels", "status", ref], env)

    def pull(self, ref: str, dest: Path) -> None:
        """Download source + kernel-metadata.json (``-m``) for an existing kernel."""
        dest.mkdir(parents=True, exist_ok=True)
        with credentials_env(self.creds) as env:
            _run(["kernels", "pull", ref, "-p", str(dest), "-m"], env)

    def logs(self, ref: str) -> str:
        """Execution log text for the latest run.

        Chosen over `kaggle kernels output` on purpose. `output` downloads every
        artifact the run produced — for a real notebook that is routinely
        gigabytes — and Supabase's free tier allows 1 GB of storage *in total*,
        so pulling output directories into it would fill the project and break
        everything else. This returns just the log, which is the part that
        answers "what did this run do?".
        """
        with credentials_env(self.creds) as env:
            return _run(["kernels", "logs", ref], env, timeout=300)

    # ---- write ---------------------------------------------------------

    def push(self, folder: Path, timeout_seconds: int | None = None) -> str:
        """Upload the folder and let Kaggle run it.

        ``--timeout`` becomes Kaggle's own session limit. The CLI documents it as
        "Limit run time, **bounded by Kaggle's maximum**" — so a value above the
        platform limit (12h CPU/GPU, 9h TPU) is clamped by Kaggle, not honoured.
        We therefore always set it slightly *above* our watchdog deadline and
        below the platform ceiling, making the watchdog the intended stop and
        Kaggle's limit only the backstop.
        """
        args = ["kernels", "push", "-p", str(folder)]
        if timeout_seconds:
            args += ["--timeout", str(int(timeout_seconds))]
        with credentials_env(self.creds) as env:
            return _run(args, env, timeout=600)


# ---- status vocabulary -------------------------------------------------
# The exact strings, confirmed from the shipped kagglesdk
# `kernels_enums.KernelWorkerStatus` (0.1.37) on 2026-09-26:
#
#   QUEUED=0  RUNNING=1  COMPLETE=2  ERROR=3
#   CANCEL_REQUESTED=4  CANCEL_ACKNOWLEDGED=5  NEW_SCRIPT=6
#
# `kernels status` prints them through Python's default Enum.__str__, so the
# real output line is e.g. `owner/slug has status "KernelWorkerStatus.RUNNING"`.
# Matching the enum token exactly is therefore safe — with a keyword fallback so
# a future rename degrades to "works" instead of "breaks".
#
# `CANCEL_REQUESTED` is NOT terminal: it means the request was received and the
# run is still winding down. Only `CANCEL_ACKNOWLEDGED` means it has stopped.

_STATUS_BY_ENUM = {
    "KernelWorkerStatus.QUEUED": "queued",
    "KernelWorkerStatus.RUNNING": "running",
    "KernelWorkerStatus.COMPLETE": "completed",
    "KernelWorkerStatus.ERROR": "failed",
    "KernelWorkerStatus.CANCEL_REQUESTED": "cancelling",
    "KernelWorkerStatus.CANCEL_ACKNOWLEDGED": "cancelled",
    "KernelWorkerStatus.NEW_SCRIPT": "not_run",
}

#: States a poll loop should stop polling on.
#:
#: `not_run` (NEW_SCRIPT) is deliberately NOT here. It means "this version was
#: saved without being executed", which is what `push --no-run` produces — but
#: it can also appear for a moment right after a normal push, before the run
#: picks up. Treating it as terminal would fail a run that is about to start, so
#: it is left for the watch window and `reap` to settle instead.
#: `cancelling` is likewise absent: the request was received, the run is still
#: winding down.
TERMINAL = {"completed", "failed", "cancelled"}

_ENUM_RE = re.compile(r"KernelWorkerStatus\.([A-Z_]+)")


def classify_status(raw: str) -> str:
    """Map a raw `kernels status` line to the runner's own vocabulary."""
    text = raw.strip()
    if not text:
        return "unknown(empty)"

    match = _ENUM_RE.search(text)
    if match and match.group(0) in _STATUS_BY_ENUM:
        return _STATUS_BY_ENUM[match.group(0)]

    # Fallback for a changed or unknown format. Order matters: inspect the
    # negative cases before the positive ones that contain them as substrings.
    lowered = text.lower()
    if "cancel" in lowered:
        # "requested" means still winding down; otherwise treat as stopped.
        return "cancelling" if "request" in lowered and "acknowledg" not in lowered else "cancelled"
    if "error" in lowered or "fail" in lowered:
        return "failed"
    if "incomplete" in lowered:
        return "running"
    if "complete" in lowered or "success" in lowered:
        return "completed"
    if "running" in lowered or "started" in lowered:
        return "running"
    if "queued" in lowered or "pending" in lowered:
        return "queued"
    return f"unknown({text[:120]})"