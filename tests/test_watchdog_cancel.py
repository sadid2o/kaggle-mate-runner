"""The cancel channel: what goes into a notebook, and what must never go in.

The injected block is *generated Python that runs on someone else's servers*,
so it is tested by executing it rather than by reading it. Two properties
matter and neither is visible from a syntax check:

* the deadline stop actually fires, and
* a cancel is only ever armed with the publishable key — a secret key must be
  refused loudly, because the notebook it would be embedded in is public code.
"""

import json
import subprocess
import sys
import textwrap

import pytest

from runner.watchdog import (
    MARKER,
    CancelChannel,
    _source,
    inject,
)

from datetime import datetime, timedelta, timezone


def now():
    return datetime.now(timezone.utc)


# ---- what must never be embedded -----------------------------------------


def test_a_secret_key_is_refused_outright():
    """A notebook is public code; a secret key would hand over the project."""
    channel = CancelChannel(
        url="https://x.supabase.co/rest/v1/rpc/km_cancel_poll",
        api_key="sb_secret_do_not_ship_me",
        job_id="11111111-1111-4111-8111-111111111111",
    )
    with pytest.raises(ValueError, match="secret key"):
        inject(b"print(1)\n", "n.py", "python", now(), channel)


def test_a_publishable_key_is_accepted():
    channel = CancelChannel(
        url="https://x.supabase.co/rest/v1/rpc/km_cancel_poll",
        api_key="sb_publishable_ok_to_ship",
        job_id="11111111-1111-4111-8111-111111111111",
    )
    out = inject(b"print(1)\n", "n.py", "python", now(), channel)
    assert out is not None
    assert b"sb_publishable_ok_to_ship" in out


def test_no_channel_means_no_key_at_all():
    """Without a cancel channel the block must carry no credential whatsoever."""
    out = inject(b"print(1)\n", "n.py", "python", now())
    text = out.decode()
    assert "apikey" not in text
    assert "urllib" not in text
    assert "sb_publishable" not in text
    assert "sb_secret" not in text


# ---- the deadline actually fires -----------------------------------------


def test_the_deadline_stop_really_exits_the_process():
    """Executed for real, in a subprocess, with a deadline already in the past.

    Reading the source proves nothing here — the failure mode this guards
    against is a watchdog thread that is started but never fires, which looks
    identical on the page and would let a run sail past its stop time.
    """
    deadline = now() - timedelta(seconds=1)
    block = _source(deadline)

    program = textwrap.dedent(
        """
        import time
        """
    ) + block + textwrap.dedent(
        """
        # If the watchdog works, the process is gone before this finishes.
        time.sleep(30)
        print("WATCHDOG DID NOT FIRE")
        """
    )

    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=25,
    )

    assert "deadline reached" in proc.stdout, proc.stdout + proc.stderr
    assert "WATCHDOG DID NOT FIRE" not in proc.stdout


def test_a_future_deadline_does_not_fire_early():
    """The opposite failure: a watchdog that kills a healthy run immediately."""
    deadline = now() + timedelta(hours=1)
    block = _source(deadline)

    program = block + textwrap.dedent(
        """
        import time
        time.sleep(2)
        print("SURVIVED")
        """
    )

    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert "SURVIVED" in proc.stdout


def test_a_network_failure_never_stops_the_notebook():
    """The cancel poll is a courtesy; the deadline is the guarantee.

    Pointed at an unroutable address so the poll always raises. The notebook
    must keep running, because losing the deadline stop to a network blip would
    let a 6-hour GPU job run to Kaggle's 12-hour cap.
    """
    channel = CancelChannel(
        url="http://127.0.0.1:9/rest/v1/rpc/km_cancel_poll",  # discard port, always fails
        api_key="sb_publishable_x",
        job_id="11111111-1111-4111-8111-111111111111",
    )
    block = _source(now() + timedelta(hours=1), channel)

    program = block + textwrap.dedent(
        """
        import time
        time.sleep(3)
        print("STILL RUNNING DESPITE NETWORK FAILURE")
        """
    )

    proc = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert "STILL RUNNING DESPITE NETWORK FAILURE" in proc.stdout, proc.stdout + proc.stderr


# ---- the generated code is well-formed -----------------------------------


def test_generated_block_is_valid_python():
    import ast

    channel = CancelChannel(
        url="https://x.supabase.co/rest/v1/rpc/km_cancel_poll",
        api_key="sb_publishable_x",
        job_id="11111111-1111-4111-8111-111111111111",
    )
    for block in (_source(now()), _source(now(), channel)):
        ast.parse(block)


def _headers_sent_for(api_key: str) -> dict:
    """Run the generated poll against a local server and capture its headers.

    Checking the *source text* for an Authorization header cannot work: the line
    is always present in the template and merely skipped at runtime. The only
    honest way to know what a notebook actually sends is to let it send it, so
    this starts a throwaway HTTP server, points the generated block at it, and
    reads back the request headers.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            captured.update(dict(self.headers))
            body = b"false"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep the test output clean

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    channel = CancelChannel(
        url=f"http://127.0.0.1:{port}/rest/v1/rpc/km_cancel_poll",
        api_key=api_key,
        job_id="11111111-1111-4111-8111-111111111111",
    )
    block = _source(now() + timedelta(hours=1), channel)

    program = block + textwrap.dedent(
        """
        import time
        # Call the poll once directly, then exit before the thread's own sleep.
        print("RESULT:", _km_cancel_requested())
        """
    )
    subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, timeout=30)
    thread.join(timeout=10)
    server.server_close()

    # BaseHTTPRequestHandler capitalises names; normalise for a case-insensitive read.
    return {k.lower(): v for k, v in captured.items()}


def test_a_modern_key_is_sent_on_apikey_only():
    """`sb_publishable_...` is not a JWT; Supabase rejects it as a Bearer token."""
    headers = _headers_sent_for("sb_publishable_modern_key")
    assert headers.get("apikey") == "sb_publishable_modern_key"
    assert "authorization" not in headers


def test_a_legacy_jwt_key_also_gets_a_bearer_header():
    """Legacy anon keys ARE JWTs and still need Authorization, so both work."""
    headers = _headers_sent_for("aaa.bbb.ccc")
    assert headers.get("apikey") == "aaa.bbb.ccc"
    assert headers.get("authorization") == "Bearer aaa.bbb.ccc"


def test_the_cancel_url_targets_the_rpc_function():
    channel = CancelChannel(
        url="https://proj.supabase.co/rest/v1/rpc/km_cancel_poll",
        api_key="sb_publishable_x",
        job_id="22222222-2222-4222-8222-222222222222",
    )
    out = _source(now(), channel)
    assert "km_cancel_poll" in out
    assert "22222222-2222-4222-8222-222222222222" in out


def test_injection_into_a_notebook_stays_first_and_carries_the_marker():
    nb = {
        "cells": [
            {"cell_type": "code", "execution_count": 3, "metadata": {}, "outputs": [{"x": 1}], "source": ["x", "= 1"]},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    channel = CancelChannel(
        url="https://x.supabase.co/rest/v1/rpc/km_cancel_poll",
        api_key="sb_publishable_x",
        job_id="33333333-3333-4333-8333-333333333333",
    )
    out = inject(json.dumps(nb).encode(), "n.ipynb", "python", now(), channel)
    parsed = json.loads(out)
    # Notebook `source` is a list of lines, so it has to be joined before the
    # marker can be looked for — `in` on a list does exact-element matching.
    first_cell = "".join(parsed["cells"][0]["source"])
    assert MARKER in first_cell
    assert parsed["cells"][0]["metadata"].get("kaggle_mate") == MARKER
    # The user's own cell is preserved, with outputs cleared as Kaggle would.
    assert parsed["cells"][1]["outputs"] == []
    assert parsed["cells"][1]["execution_count"] is None