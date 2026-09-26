"""The watchdog must be injected invisibly — and must actually work.

`syntax` is not a stylistic choice of name: the injected block goes into a
notebook the user wrote, so a syntax error there would break their run. Where
the `ast` module is available we check that properly; otherwise we still assert
the properties that can be checked without it.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from runner import watchdog
from runner.push_builder import PushOverrides, build_push_folder, parse_metadata
from runner.watchdog import MARKER, inject

DEADLINE = datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)


def notebook(cells=None) -> bytes:
    return json.dumps(
        {
            "cells": cells
            if cells is not None
            else [
                {
                    "cell_type": "code",
                    "execution_count": 3,
                    "metadata": {},
                    "outputs": [{"output_type": "stream", "text": "stale output\n"}],
                    "source": ["print('hello')"],
                }
            ],
            "metadata": {"language_info": {"name": "python"}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    ).encode("utf-8")


# ---- injection -----------------------------------------------------------


def test_watchdog_is_first_cell():
    out = json.loads(inject(notebook(), "nb.ipynb", "python", DEADLINE))
    assert out["cells"][0]["metadata"]["kaggle_mate"] == MARKER
    assert "print('hello')" in "".join(out["cells"][1]["source"])


def test_original_cell_survives_unchanged():
    out = json.loads(inject(notebook(), "nb.ipynb", "python", DEADLINE))
    body = out["cells"][1]
    assert "".join(body["source"]) == "print('hello')"


def test_existing_outputs_are_cleared():
    """kaggle kernels push rejects a notebook that still carries outputs."""
    out = json.loads(inject(notebook(), "nb.ipynb", "python", DEADLINE))
    body = out["cells"][1]
    assert body["outputs"] == []
    assert body["execution_count"] is None


def test_list_source_is_joined():
    """Kaggle's parser prefers a single source string."""
    out = json.loads(inject(notebook(), "nb.ipynb", "python", DEADLINE))
    assert isinstance(out["cells"][1]["source"], str)


def test_deadline_is_embedded_as_iso():
    raw = inject(notebook(), "nb.ipynb", "python", DEADLINE).decode()
    assert DEADLINE.isoformat() in raw


def test_py_file_gets_the_block_prepended():
    out = inject(b"print('hi')\n", "run.py", "python", DEADLINE).decode()
    assert out.startswith("# ----")
    assert out.rstrip().endswith("print('hi')")


def test_rmd_is_left_alone():
    """An R Markdown file cannot host a Python watchdog — caller is told, not lied to."""
    assert inject(b"---\ntitle: x\n---\n", "run.Rmd", "r", DEADLINE) is None


def test_injected_block_has_no_syntax_error():
    pytest.importorskip("ast")
    import ast

    out = inject(b"print('hi')\n", "run.py", "python", DEADLINE)
    ast.parse(out.decode())  # raises SyntaxError if the injected block is broken


def test_injected_python_block_defines_the_watchdog():
    out = inject(b"", "run.py", "python", DEADLINE).decode()
    assert "def _km_watchdog" in out
    assert "_km_th.Thread" in out


def test_deadline_is_timezone_aware_in_generated_code():
    """A naive comparison against now(utc) would raise at runtime."""
    out = inject(b"", "run.py", "python", DEADLINE).decode()
    assert "_KM_DEADLINE" in out
    assert "timezone.utc" in out or "tzinfo" in out


def test_already_injected_detects_marker():
    assert watchdog.already_injected(b"# kaggle-mate-watchdog\n", "run.py")
    assert not watchdog.already_injected(b"print(1)\n", "run.py")


# ---- push folder ---------------------------------------------------------


def test_push_folder_contains_both_files(tmp_path):
    plan = build_push_folder(
        dest=tmp_path,
        code_bytes=notebook(),
        code_file="nb.ipynb",
        metadata={"language": "python", "code_file": "nb.ipynb"},
        deadline_utc=DEADLINE,
    )
    assert (tmp_path / "nb.ipynb").exists()
    assert (tmp_path / "kernel-metadata.json").exists()
    assert plan.watchdog_injected is True


def test_metadata_booleans_are_normalised():
    """Kaggle writes "false" as a string; `if meta['enable_gpu']` would be wrong."""
    meta = parse_metadata(b'{"enable_gpu": "false", "enable_tpu": "true", "title": "t"}')
    assert meta["enable_gpu"] is False
    assert meta["enable_tpu"] is True


def test_overrides_do_not_disturb_other_fields():
    meta = {"language": "python", "title": "keep me", "dataset_sources": ["a/b"]}
    out = PushOverrides(enable_gpu=True).apply(meta)
    assert out["title"] == "keep me"
    assert out["dataset_sources"] == ["a/b"]
    assert out["enable_gpu"] == "true"