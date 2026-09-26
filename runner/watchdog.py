"""Injected watchdog — the honest half of "stop at a given time".

Kaggle has no supported way to stop a run that ``kaggle kernels push`` started.
That is verified, not assumed: the SDK's ``cancel_kernel_session`` exists and its
endpoint is ``/api/v1/kernels/cancel-session/{kernel_session_id}``, but it needs
a ``kernel_session_id`` that nothing public returns. ``push`` answers with
``ref/url/version_number/kernel_id``; ``get_kernel_session_status`` answers with
``status`` and ``failure_message``; ``create_kernel_session`` answers with a
long-running ``Operation``; and every call the CLI makes resolves
``owner/slug`` server-side. So the only thing that can reliably stop a run is
code running *inside* it.

Two things can therefore stop a run, and both live here:

* the **deadline**, known before the push and needing nothing but the clock.
  This is what serves the user's two scheduled stop rules — "after N hours" and
  "at a clock time" — and it works with the internet switched off.
* a **cancel request**, which a running notebook can only hear by asking. That
  needs the internet and a credential inside the notebook, so it is opt-in, it
  is skipped entirely when internet is off, and it uses the *publishable* key
  only. The secret key never goes near a notebook — see the guard in `inject`.

The user's stored notebook is never modified: the watchdog goes into a generated
copy built at push time, and the stored version stays byte-exact in Storage.

This is a workaround, not a Kaggle feature. Phase 0 measures whether Kaggle
reports such a run as ``error`` (it may — ``os._exit`` is abrupt) so the runner
can classify an expected deadline stop as ``stopped`` rather than a failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

MARKER = "kaggle-mate-watchdog"

#: Languages the watchdog can be injected into. R Markdown cannot host a Python
#: watchdog thread, so .Rmd runs are limited to Kaggle's own session timeout —
#: a real limitation, recorded here rather than hidden.
SUPPORTED_LANGUAGES = {"python"}

#: Prefix of the Supabase secret-key formats. Never allowed inside a notebook.
_SECRET_PREFIXES = ("sb_secret_",)


@dataclass(frozen=True)
class CancelChannel:
    """How a running notebook hears "stop now" from the app.

    The job UUID is the real capability here, not the key. It is a v4 UUID and
    therefore unguessable, and the endpoint it is sent to answers with a single
    boolean for that one id and nothing else — no job data, no account data. The
    API key beside it is the *publishable* key, which Supabase designs to be
    public. Worst case if a notebook is published with this in it: a reader can
    learn whether one already-finished job was cancelled.
    """

    url: str
    api_key: str
    job_id: str


# ---- generated source ----------------------------------------------------

# Kept as explicit templates rather than an f-string: the generated code is full
# of `{` and `}` (dict literals), which an f-string would try to interpret.

_HEAD = '''# ---- {marker} (auto-injected; the original notebook is untouched) ----
import datetime as _km_dt, os as _km_os, threading as _km_th, time as _km_t

_KM_DEADLINE = _km_dt.datetime.fromisoformat("{deadline}")


def _km_stop(_km_reason):
    print("kaggle-mate: " + _km_reason + ", stopping this session", flush=True)
    _km_os._exit(0)
'''

_CANCEL = '''
import json as _km_json, urllib.request as _km_urllib

_KM_CANCEL_URL = "{url}"
_KM_CANCEL_KEY = "{key}"
_KM_JOB_ID = "{job_id}"


def _km_cancel_requested():
    _km_headers = {{"apikey": _KM_CANCEL_KEY, "Content-Type": "application/json"}}
    # New-format keys (sb_publishable_...) are NOT JWTs and are rejected when
    # sent as a Bearer token, which is a documented Supabase behaviour. Legacy
    # anon keys are JWTs and do need it. So Bearer is added only for a
    # JWT-shaped key, and both formats work without a version check.
    if _KM_CANCEL_KEY.count(".") == 2:
        _km_headers["Authorization"] = "Bearer " + _KM_CANCEL_KEY
    _km_req = _km_urllib.Request(
        _KM_CANCEL_URL,
        data=_km_json.dumps({{"p_job_id": _KM_JOB_ID}}).encode("utf-8"),
        headers=_km_headers,
    )
    with _km_urllib.urlopen(_km_req, timeout=15) as _km_resp:
        return bool(_km_json.loads(_km_resp.read().decode("utf-8")))
'''

_LOOP_DEADLINE_ONLY = '''

def _km_watchdog():
    while True:
        if _km_dt.datetime.now(_km_dt.timezone.utc) >= _KM_DEADLINE:
            _km_stop("deadline reached")
        _km_t.sleep(30)


_km_th.Thread(target=_km_watchdog, daemon=True).start()
# ---- end {marker} ----
'''

_LOOP_WITH_CANCEL = '''

def _km_watchdog():
    while True:
        if _km_dt.datetime.now(_km_dt.timezone.utc) >= _KM_DEADLINE:
            _km_stop("deadline reached")
        try:
            if _km_cancel_requested():
                _km_stop("cancel requested")
        except Exception:
            # No internet route, or Supabase unreachable. The deadline check
            # above is the guarantee; this poll is only an early-stop courtesy,
            # so a network failure must never stop the notebook.
            pass
        _km_t.sleep(30)


_km_th.Thread(target=_km_watchdog, daemon=True).start()
# ---- end {marker} ----
'''


def _source(deadline_utc: datetime, cancel: CancelChannel | None = None) -> str:
    deadline = deadline_utc.astimezone(timezone.utc)
    parts = [_HEAD.format(marker=MARKER, deadline=deadline.isoformat())]

    if cancel is None:
        parts.append(_LOOP_DEADLINE_ONLY.format(marker=MARKER))
    else:
        parts.append(
            _CANCEL.format(url=cancel.url, key=cancel.api_key, job_id=cancel.job_id)
        )
        parts.append(_LOOP_WITH_CANCEL.format(marker=MARKER))

    return "".join(parts)


# ---- public API ----------------------------------------------------------


def inject(
    content: bytes,
    code_file: str,
    language: str,
    deadline_utc: datetime,
    cancel: CancelChannel | None = None,
) -> bytes | None:
    """Return the push-ready bytes, or ``None`` when injection is not possible.

    ``None`` is a normal outcome, not an error: the caller then relies on
    Kaggle's ``session_timeout_seconds`` alone and records that the job ran
    without a watchdog.
    """
    if language not in SUPPORTED_LANGUAGES:
        return None

    if cancel is not None and cancel.api_key.startswith(_SECRET_PREFIXES):
        # A notebook pushed to Kaggle is readable there, and a user may publish
        # it. A secret key would hand over the whole project, so this is a hard
        # stop rather than a warning.
        raise ValueError(
            "refusing to embed a Supabase secret key in a notebook; "
            "the cancel channel must use the publishable key"
        )

    block = _source(deadline_utc, cancel)
    lowered = code_file.lower()

    if lowered.endswith(".ipynb"):
        return _inject_ipynb(content, block)
    if lowered.endswith(".py"):
        return block.encode("utf-8") + content
    return None


def _inject_ipynb(content: bytes, block: str) -> bytes:
    """Insert the watchdog as the first code cell of the notebook.

    Outputs are cleared and list-form ``source`` is joined, which is exactly the
    normalisation Kaggle's own push applies before sending a notebook — doing it
    here too means the stored version and the pushed version agree.
    """
    import json

    nb = json.loads(content)

    for cell in nb.get("cells", []):
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
            src = cell.get("source")
            if isinstance(src, list):
                cell["source"] = "".join(src)

    nb.setdefault("cells", []).insert(
        0,
        {
            "cell_type": "code",
            "execution_count": None,
            "metadata": {"kaggle_mate": MARKER},
            "outputs": [],
            "source": block.splitlines(keepends=True),
        },
    )
    return json.dumps(nb).encode("utf-8")


def already_injected(content: bytes, code_file: str) -> bool:
    """Guard against injecting twice into the same notebook."""
    return MARKER in content.decode("utf-8", errors="ignore")