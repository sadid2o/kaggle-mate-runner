"""Status parsing and credential handling.

`classify_status` gates every decision the runner makes — whether a job is
still going, finished, or dead — so it is tested against the *real* enum values
rather than invented ones. The vocabulary below was read from the shipped
`kagglesdk` (`kernels_enums.KernelWorkerStatus`, 0.1.37) and from the CLI's own
`print('%s has status "%s"')` call, which renders the enum through Python's
default `Enum.__str__` as `KernelWorkerStatus.RUNNING`.
"""

import json

import pytest

from runner.kaggle_cli import (
    TERMINAL,
    KaggleCredentials,
    classify_status,
    credentials_env,
)


def cli_line(status: str) -> str:
    """Exactly the shape `kaggle kernels status` prints."""
    return f'owner/slug has status "KernelWorkerStatus.{status}"'


# ---- the real vocabulary -------------------------------------------------


@pytest.mark.parametrize(
    "enum_name,expected",
    [
        ("QUEUED", "queued"),
        ("RUNNING", "running"),
        ("COMPLETE", "completed"),
        ("ERROR", "failed"),
        ("CANCEL_REQUESTED", "cancelling"),
        ("CANCEL_ACKNOWLEDGED", "cancelled"),
        ("NEW_SCRIPT", "not_run"),
    ],
)
def test_every_real_enum_value_maps(enum_name, expected):
    """All seven states in the shipped enum, not a guessed subset."""
    assert classify_status(cli_line(enum_name)) == expected


def test_running_is_not_confused_with_its_prefixes():
    """`RUNNING` must not be read as `COMPLETE` or vice versa."""
    assert classify_status(cli_line("RUNNING")) == "running"
    assert classify_status(cli_line("COMPLETE")) == "completed"


def test_cancel_requested_is_not_terminal_but_acknowledged_is():
    """The distinction matters: requested = winding down, acknowledged = stopped.

    Treating `CANCEL_REQUESTED` as an end would let the worker finalise a job
    while the notebook is still running.
    """
    assert classify_status(cli_line("CANCEL_REQUESTED")) == "cancelling"
    assert "cancelling" not in TERMINAL
    assert classify_status(cli_line("CANCEL_ACKNOWLEDGED")) == "cancelled"
    assert "cancelled" in TERMINAL


def test_not_run_is_not_terminal():
    """`NEW_SCRIPT` can appear briefly after a normal push.

    It means "version saved, not executed". A normal push may briefly report it
    before the run picks up, so ending the watch on it would fail a live run.
    """
    assert classify_status(cli_line("NEW_SCRIPT")) == "not_run"
    assert "not_run" not in TERMINAL


def test_terminal_set_contains_only_real_end_states():
    assert TERMINAL == {"completed", "failed", "cancelled"}


# ---- robustness to a changed format --------------------------------------


def test_failure_message_line_does_not_break_parsing():
    """`status` prints a second line when there is a failure message."""
    raw = cli_line("ERROR") + '\nFailure message: "CUDA out of memory"'
    assert classify_status(raw) == "failed"


def test_plain_words_still_work_if_the_enum_format_changes():
    """A keyword fallback means a format change degrades, not breaks."""
    assert classify_status("queued") == "queued"
    assert classify_status("running") == "running"
    assert classify_status("complete") == "completed"
    assert classify_status("errored") == "failed"


def test_incomplete_is_running_not_complete():
    """`incomplete` contains `complete` — order of checks matters."""
    assert classify_status("INCOMPLETE") == "running"


def test_empty_input_is_reported_not_crashed():
    assert classify_status("   ") == "unknown(empty)"


def test_unrecognised_text_is_surfaced_verbatim():
    """An unknown state must be visible, never silently mapped to a real one."""
    out = classify_status("some brand new state")
    assert out.startswith("unknown(")
    assert "brand new state" in out


# ---- credentials ---------------------------------------------------------


def test_bare_token_is_treated_as_a_modern_api_token():
    creds = KaggleCredentials.from_secret("KGAT_abc123")
    assert creds.username is None
    assert creds.key == "KGAT_abc123"


def test_username_colon_key_is_parsed_as_legacy():
    creds = KaggleCredentials.from_secret("sadid:deadbeef")
    assert creds.username == "sadid"
    assert creds.key == "deadbeef"


def test_kaggle_json_blob_is_accepted_verbatim():
    blob = json.dumps({"username": "sadid", "key": "deadbeef"})
    creds = KaggleCredentials.from_secret(blob)
    assert creds.username == "sadid"
    assert creds.key == "deadbeef"


def test_a_token_env_carries_no_config_dir(monkeypatch):
    """The modern path writes no file at all."""
    creds = KaggleCredentials(key="KGAT_xyz", username=None)
    with credentials_env(creds) as env:
        assert env["KAGGLE_API_TOKEN"] == "KGAT_xyz"
        assert "KAGGLE_CONFIG_DIR" not in env


def test_legacy_env_points_at_an_isolated_dir(monkeypatch):
    creds = KaggleCredentials(key="deadbeef", username="sadid")
    with credentials_env(creds) as env:
        assert env["KAGGLE_CONFIG_DIR"]
        assert env["KAGGLE_API_TOKEN"] if False else True  # not set below
        assert "KAGGLE_API_TOKEN" not in env


def test_outer_environment_cannot_shadow_the_credential(monkeypatch):
    """A stray KAGGLE_KEY in the environment must not leak into a call.

    Environment variables take precedence over kaggle.json in the CLI, so an
    inherited one would silently make every account use the same token.
    """
    monkeypatch.setenv("KAGGLE_KEY", "wrong-token")
    monkeypatch.setenv("KAGGLE_USERNAME", "wrong-user")
    monkeypatch.setenv("KAGGLE_API_TOKEN", "wrong-token-too")

    creds = KaggleCredentials(key="KGAT_right", username=None)
    with credentials_env(creds) as env:
        assert env["KAGGLE_API_TOKEN"] == "KGAT_right"
        assert "KAGGLE_KEY" not in env
        assert "KAGGLE_USERNAME" not in env


def test_temp_config_is_removed_after_use():
    """The credential must not survive on disk after the call."""
    from pathlib import Path

    creds = KaggleCredentials(key="deadbeef", username="sadid")
    with credentials_env(creds) as env:
        path = Path(env["KAGGLE_CONFIG_DIR"])
        assert path.exists()
    assert not path.exists()