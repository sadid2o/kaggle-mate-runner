"""The contract between the runner's failure codes and the app's explanations.

**The bug this exists for.** The runner writes ten codes. The app translated five
entirely different ones (`kaggle_auth`, `kaggle_push_failed`, `timeout`,
`notebook_never_started`, `runner_error`), none of which the runner has ever
written. The overlap was **zero** — every real failure fell through to a generic
"The log page will show the full reason", and the five strings the app knew were
dead. Nothing failed; there was simply nothing that could fail. Two languages,
two checkouts, no shared type.

**What replaces the missing type checker.** These tests scan the runner's own
source — Python, TypeScript and SQL — for every code and stop reason it can
actually write, and compare that against the vocabulary the app declares in
`lib/core/error/runner_failure.dart`. They run in both directions, because the
two halves of the original bug were different:

* a code the runner writes and the app does not explain is a user seeing a raw
  constant, or a vague message, for something we knew about; and
* a code the app explains and nothing writes is dead weight that makes the
  vocabulary look healthier than it is.

Neither is a syntax error, so `deno check`, `flutter analyze` and `pytest` all
pass while it happens. That is the whole point.

**Where the app lives.** The two checkouts are siblings, and the runner's CI
only ever sees this repository. So the app's vocabulary is committed as a
snapshot at `contract/runner_failure.json`; the CI tests below check the
snapshot against a frozen list, and the full comparison runs wherever both
checkouts are present (locally, and on any machine with the app cloned next to
this one). A snapshot that could drift from the Dart file unnoticed would be
worse than no snapshot, so two of its fields — the file hash and the code list —
are pinned here by hand.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    # `python -m pytest` already puts the working directory on the path; a bare
    # `pytest` from elsewhere does not, and this file is imported before any
    # test runs.
    sys.path.insert(0, str(REPO_ROOT))

from tools.scan_error_codes import (  # noqa: E402
    CONTRACT_PATH,
    DART_VOCABULARY_PATH,
    DEFAULT_APP_ROOT,
    dart_file_hash,
    parse_dart_vocabulary,
    scan_python_source,
    scan_repo,
    scan_sql_source,
    scan_typescript_source,
)

APP_ROOT = Path(os.environ.get("KM_APP_ROOT", DEFAULT_APP_ROOT))
APP_AVAILABLE = (APP_ROOT / DART_VOCABULARY_PATH).is_file()

needs_app = pytest.mark.skipif(
    not APP_AVAILABLE,
    reason=(
        f"the Flutter app is not checked out at {APP_ROOT}; run with "
        "KM_APP_ROOT=<path> to point at it. The runner-side and snapshot "
        "checks below still run without it."
    ),
)

#: The vocabulary as reviewed. Bumping this is a deliberate act: it means a
#: person read the new code's explanation and agreed with it. It is duplicated
#: from the Dart file on purpose — if the snapshot were regenerated from a
#: wrong Dart file, a hash check alone would happily agree with itself.
FROZEN_CODES = frozenset(
    {
        "AUTH_FAILED",
        "PUSH_FAILED",
        "QUOTA_LIMIT",
        "KAGGLE_RUN_ERROR",
        "WORKER_CRASH",
        "MISSED_WINDOW",
        "DEADLINE_STOP_UNCONFIRMED",
        "CANCELLED_BY_USER",
        "OUTPUT_FETCH_FAILED",
        "CANCEL_UNCONFIRMED",
    }
)

#: Codes written to the `jobs` row — the only ones a run screen can receive.
FROZEN_RUN_LEVEL_CODES = frozenset(
    {
        "AUTH_FAILED",
        "PUSH_FAILED",
        "QUOTA_LIMIT",
        "KAGGLE_RUN_ERROR",
        "WORKER_CRASH",
        "MISSED_WINDOW",
        "DEADLINE_STOP_UNCONFIRMED",
        "CANCELLED_BY_USER",
    }
)

#: Codes written to `job_events` only.
FROZEN_EVENT_ONLY_CODES = frozenset({"OUTPUT_FETCH_FAILED", "CANCEL_UNCONFIRMED"})

#: How the app treats each code. `cancelled` is the load-bearing one: the run
#: screen used to show a red "Why it failed" card whenever `error_code` was
#: non-null, so cancelling a pending job told the user their own action failed.
FROZEN_SEVERITIES = {
    "AUTH_FAILED": "failure",
    "PUSH_FAILED": "failure",
    "QUOTA_LIMIT": "failure",
    "KAGGLE_RUN_ERROR": "failure",
    "WORKER_CRASH": "failure",
    "MISSED_WINDOW": "failure",
    "DEADLINE_STOP_UNCONFIRMED": "failure",
    "CANCELLED_BY_USER": "cancelled",
    "OUTPUT_FETCH_FAILED": "notice",
    "CANCEL_UNCONFIRMED": "notice",
}


@pytest.fixture(scope="session")
def scanned():
    """One scan of the runner, shared by every test that needs it."""
    return scan_repo()


@pytest.fixture(scope="session")
def contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def declared():
    """The app's vocabulary, or `None` when the app is not checked out here."""
    if not APP_AVAILABLE:
        return None
    return parse_dart_vocabulary(APP_ROOT)


# ------------------------------------------------- the contract, both ways


def test_the_scanner_reads_every_write_it_finds(scanned):
    """No write may be skipped silently.

    An unreadable `error_code` — a value computed from a config dict, a helper
    that writes neither table — would otherwise contribute nothing and pass. An
    unread write is precisely the shape of the original bug, so it has to fail
    loudly instead.
    """
    assert scanned.unresolved == [], (
        "the scanner could not read these writes, so it cannot vouch for the "
        "vocabulary:\n" + "\n".join(f"  - {item}" for item in scanned.unresolved)
    )


def test_the_scanner_finds_the_codes_it_is_expected_to(scanned):
    """A control on the scanner itself.

    If an edit stopped it reaching the runner's sources, every other test here
    would compare two empty sets and pass. Pinning the known set makes that
    failure mode impossible.
    """
    assert scanned.job_row_codes == FROZEN_RUN_LEVEL_CODES
    assert scanned.event_codes == FROZEN_EVENT_ONLY_CODES


@needs_app
def test_every_code_the_runner_writes_is_explained_by_the_app(scanned, declared):
    """Direction one: nothing the runner can write may be unexplained."""
    unexplained = scanned.codes - declared.codes
    assert not unexplained, (
        "the runner writes these codes and the app has no explanation for them, "
        "so a user would see the raw constant:\n"
        + "\n".join(f"  - {code}" for code in sorted(unexplained))
        + f"\n\nAdd them to {DART_VOCABULARY_PATH} "
        f"({', '.join(sorted(scanned.codes))})."
    )


@needs_app
def test_the_app_explains_no_code_that_nothing_writes(scanned, declared):
    """Direction two: no dead codes.

    This is the half that was wrong for the longest — five codes the app
    translated and the runner had never written, which is what hid the other
    half, because the fallback looked like it was working.
    """
    dead = declared.codes - scanned.codes
    assert not dead, (
        "the app explains these codes but nothing in the runner writes them, so "
        "their explanations can never be reached:\n"
        + "\n".join(f"  - {code}" for code in sorted(dead))
    )


@needs_app
def test_the_run_level_and_event_only_split_matches(scanned, declared):
    """Where a code lands is part of its meaning, not a filing detail.

    A code filtered to the wrong half is a real defect: `OUTPUT_FETCH_FAILED`
    would put a red failure card on a run that succeeded, and
    `DEADLINE_STOP_UNCONFIRMED` would be hidden from the one screen meant to
    warn about it.
    """
    assert declared.run_level_codes == scanned.job_row_codes, (
        "the app's run-level codes do not match what the runner writes to the "
        "jobs row"
    )
    assert declared.event_only_codes == scanned.event_codes, (
        "the app's event-only codes do not match what the runner writes to "
        "job_events"
    )
    assert declared.run_level_codes | declared.event_only_codes == declared.codes
    assert not (declared.run_level_codes & declared.event_only_codes)


@needs_app
def test_every_stop_reason_the_runner_writes_has_a_label(scanned, declared):
    """The same bug in the second vocabulary.

    The app labelled `cancelled` and `watchdog` — neither of which the runner has
    ever written — while `cancelled_by_user` and `notebook_finished` went to the
    screen raw.
    """
    missing = scanned.stop_reasons - declared.stop_reasons
    assert not missing, (
        "the runner records these stop reasons with no label in the app:\n"
        + "\n".join(f"  - {reason}" for reason in sorted(missing))
    )


@needs_app
def test_the_app_labels_no_stop_reason_that_nothing_writes(scanned, declared):
    dead = declared.stop_reasons - scanned.stop_reasons
    assert not dead, (
        "the app labels these stop reasons but the runner never records them:\n"
        + "\n".join(f"  - {reason}" for reason in sorted(dead))
    )


@needs_app
def test_every_code_has_an_explanation_and_a_title(declared):
    """A code with no explanation is worse than no code, because it looks like
    information.

    Checked through the parsed vocabulary and not the snapshot, so a `_Meaning`
    entry added for a code that is missing from `codes` (or the reverse) is
    caught here — the two lists are written by hand and can disagree.
    """
    assert set(declared.severities) == declared.codes, (
        "every code needs a _Meaning entry, and every entry needs a code in "
        f"`codes`. Only in codes: {sorted(declared.codes - set(declared.severities))}; "
        f"only in _byCode: {sorted(set(declared.severities) - declared.codes)}"
    )
    assert set(declared.severities.values()) <= {"failure", "cancelled", "notice"}


# ----------------------------------------- the snapshot (runs in runner CI)


def test_the_snapshot_records_the_reviewed_vocabulary(contract):
    """The CI-side check: the committed snapshot is the one a person approved.

    The runner's CI cannot see the Dart file, so this is what stands in for the
    full comparison there. A change to the Dart vocabulary must move the snapshot
    *and* this list together, which is exactly the moment someone should be
    reading the new explanation.
    """
    assert contract["source"] == DART_VOCABULARY_PATH
    assert set(contract["codes"]) == FROZEN_CODES
    assert set(contract["run_level_codes"]) == FROZEN_RUN_LEVEL_CODES
    assert set(contract["event_only_codes"]) == FROZEN_EVENT_ONLY_CODES
    assert contract["severities"] == FROZEN_SEVERITIES


@needs_app
def test_the_snapshot_matches_the_dart_file_it_came_from(contract, declared):
    """A stale snapshot would let the CI check pass while the app had moved on."""
    assert contract["sha256"] == declared.sha256, (
        f"{DART_VOCABULARY_PATH} has changed since the snapshot was written. "
        "Regenerate it with "
        "`python tools/scan_error_codes.py --write-contract` and review the diff."
    )
    assert set(contract["codes"]) == declared.codes
    assert set(contract["run_level_codes"]) == declared.run_level_codes
    assert set(contract["event_only_codes"]) == declared.event_only_codes
    assert set(contract["stop_reasons"]) == declared.stop_reasons
    assert contract["severities"] == declared.severities


@needs_app
def test_the_snapshot_records_the_dart_files_real_hash(contract):
    """Belt and braces: recompute the hash from the file itself.

    Uses the same line-ending-independent helper the snapshot was written with —
    hashing raw bytes here would fail on a CRLF checkout of a file nobody had
    edited, which is the false alarm this pairing exists to avoid.
    """
    actual = dart_file_hash(APP_ROOT / DART_VOCABULARY_PATH)
    assert contract["sha256"] == actual


def test_the_hash_ignores_line_endings_but_not_edits(tmp_path):
    """The hash must notice a real edit and ignore a checkout difference.

    Windows' `git config core.autocrlf=true` hands out CRLF files; Linux CI hands
    out LF. Both are the same source, and a hash that disagreed would fail for a
    reason no one could act on.
    """
    lf = tmp_path / "lf.dart"
    crlf = tmp_path / "crlf.dart"
    edited = tmp_path / "edited.dart"

    body = "static const codes = <String>[\n  authFailed,\n];\n"
    lf.write_bytes(body.encode("utf-8"))
    crlf.write_bytes(body.replace("\n", "\r\n").encode("utf-8"))
    edited.write_bytes(body.replace("authFailed", "pushFailed").encode("utf-8"))

    assert dart_file_hash(lf) == dart_file_hash(crlf)
    assert dart_file_hash(lf) != dart_file_hash(edited)


# ------------------------------------------------------ supporting details


def test_the_migration_still_bounds_stop_reason_pref(scanned):
    """`stop_reason` is set from `stop_reason_pref` at runtime, so the migration's
    CHECK constraint is what actually bounds it — if the constraint were dropped,
    the scanner would resolve `job.get("stop_reason_pref")` to nothing and
    silently stop checking two of the five reasons."""
    assert scanned.stop_reason_pref_values == {"duration", "clock_time", "manual"}


def test_the_scanner_needs_nothing_but_the_standard_library():
    """The scanner is imported by this suite, which runs in CI before anything
    else is installed. `ast`, `json`, `hashlib`, `re`, `pathlib` and `dataclasses`
    are all stdlib; a third-party import added here would break the runner's whole
    test job, so it is worth failing on purpose."""
    import tools.scan_error_codes as scanner

    source = Path(scanner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    stdlib = set(sys.stdlib_module_names)
    third_party = {
        name
        for name in imported
        if name not in stdlib and name != "__future__"
    }
    assert not third_party, (
        f"the scanner imports {sorted(third_party)}, which the runner's CI does "
        "not install before running the tests"
    )


# --------------------------------------------------------------- controls
#
# Each control feeds the scanner a hand-written source whose answer is known, so
# a scanner that silently stopped working would be caught by its own tests rather
# than by a bug reaching a user.


def test_scanner_follows_an_indirect_code_assignment():
    """`QUOTA_LIMIT` and `PUSH_FAILED` never appear at the write site.

    They arrive as `error_code=code_tag`, where `code_tag` is chosen by a
    conditional a line earlier. A scanner that only read literals in argument
    position would miss both — and those are two of the codes users hit most.
    """
    scan = scan_python_source(
        """
def _finish(db, job_id, state, **patch):
    body = {"state": state, **patch}
    db.update("jobs", {"id": job_id}, body)

def start(db, job):
    try:
        push()
    except KaggleError:
        code_tag = "QUOTA_LIMIT" if looks_like_quota(stderr) else "PUSH_FAILED"
        _finish(db, job, "failed", error_code=code_tag)
""",
        "sample.py",
    )
    assert scan.job_row_codes == {"QUOTA_LIMIT", "PUSH_FAILED"}
    assert scan.unresolved == []


def test_scanner_separates_job_writes_from_event_writes():
    """The role comes from the helper's body, not from its name."""
    scan = scan_python_source(
        """
def _finish(db, job_id, state, **patch):
    db.update("jobs", {"id": job_id}, {"state": state, **patch})

def _event(db, job_id, message, **extra):
    db.insert("job_events", {"job_id": job_id, "message": message, **extra})

def run(db, job_id):
    _finish(db, job_id, "failed", error_code="WORKER_CRASH")
    _event(db, job_id, "log failed", error_code="OUTPUT_FETCH_FAILED")
""",
        "sample.py",
    )
    assert scan.job_row_codes == {"WORKER_CRASH"}
    assert scan.event_codes == {"OUTPUT_FETCH_FAILED"}


def test_scanner_reports_a_write_it_cannot_read():
    """A computed code must not vanish.

    Reporting it is the only safe answer: the alternative is a code path that
    exists, runs, and is invisible to the very test meant to catch it.
    """
    scan = scan_python_source(
        """
def _finish(db, job_id, state, **patch):
    db.update("jobs", {"id": job_id}, {"state": state, **patch})

def run(db, job_id, config):
    _finish(db, job_id, "failed", error_code=config["code"])
""",
        "sample.py",
    )
    assert scan.job_row_codes == set()
    assert len(scan.unresolved) == 1
    assert "error_code= could not be read" in scan.unresolved[0]


def test_scanner_reports_a_write_by_a_function_that_touches_neither_table():
    """Neither a job write nor an event write is not "no failure code" — it is a
    code going somewhere this test cannot classify."""
    scan = scan_python_source(
        """
def helper(db, error_code):
    db.insert("audit", {"error_code": error_code})

def run(db):
    helper(db, error_code="AUTH_FAILED")
""",
        "sample.py",
    )
    assert scan.codes == set()

    # Two separate reports, and both are real. The inner write's value is a
    # parameter, so it cannot be read; and the call site passes a literal to a
    # function whose body writes neither `jobs` nor `job_events`, so the code's
    # destination is unclassifiable. Collapsing these to one count would hide
    # whichever the scanner happened to drop.
    assert len(scan.unresolved) == 2
    assert any("dict error_code unreadable" in item for item in scan.unresolved)
    assert any("writes neither table" in item for item in scan.unresolved)


def test_scanner_resolves_stop_reason_pref_through_the_migration():
    """`stop_reason=job.get("stop_reason_pref")` is bounded by the migration."""
    scan = scan_python_source(
        """
def _finish(db, job_id, state, **patch):
    db.update("jobs", {"id": job_id}, {"state": state, **patch})

def reap(db, job):
    _finish(db, job["id"], "stopped", stop_reason=job.get("stop_reason_pref") or "duration")
""",
        "sample.py",
        {"duration", "clock_time", "manual"},
    )
    assert scan.stop_reasons == {"duration", "clock_time", "manual"}


def test_scanner_resolves_the_pref_from_a_real_migration():
    scan = scan_sql_source(
        """
alter table jobs drop constraint if exists jobs_stop_reason_pref_check;
alter table jobs
    add constraint jobs_stop_reason_pref_check
    check (stop_reason_pref in ('duration', 'clock_time', 'manual'));
""",
        "0002.sql",
    )
    assert scan.stop_reason_pref_values == {"duration", "clock_time", "manual"}


def test_scanner_reads_the_cancel_branch_of_the_claim_rpc():
    """The RPC writes `CANCELLED_BY_USER` into the jobs row when it finalises a
    pending job — a code that never passes through Python at all."""
    scan = scan_sql_source(
        """
           error_code  = case
                             when p_mode = 'cancel' and target.state in ('pending','claimed')
                             then 'CANCELLED_BY_USER'
                             else error_code
                         end,
""",
        "0001.sql",
    )
    assert scan.job_row_codes == {"CANCELLED_BY_USER"}


def test_scanner_reads_a_typescript_object_literal():
    scan = scan_typescript_source(
        """
await supabase
  .from("jobs")
  .update({
    state: "cancelled",
    error_code: "CANCELLED_BY_USER",
    stop_reason: "cancelled_by_user",
  })
  .eq("id", id);
""",
        "jobs/index.ts",
    )
    assert scan.job_row_codes == {"CANCELLED_BY_USER"}
    assert scan.stop_reasons == {"cancelled_by_user"}
    assert scan.unresolved == []


def test_scanner_does_not_mistake_stop_reason_pref_for_stop_reason():
    """`stop_reason_pref` contains `stop_reason` as a prefix.

    Left unhandled, every validation line about the *preference* would read as an
    unreadable write of the *recorded reason* — four false failures from one
    substring.
    """
    scan = scan_typescript_source(
        """
const stopReasonPref = optionalString(params, "stop_reason_pref");
if (!stopReasonPref) {
  throw badRequest('"stop_reason_pref" must be duration, clock_time or manual.');
}
row["stop_reason_pref"] = stopReasonPref;
""",
        "schedules/index.ts",
    )
    assert scan.stop_reasons == set()
    assert scan.unresolved == []


def test_scanner_ignores_a_commented_out_code():
    """A code in a comment is prose, not a write.

    The migration's own comments mention `CANCELLED_BY_USER`, and the claim RPC's
    TypeScript doc comment quotes both the code and its stop reason. Reading
    those as writes would make the app look right for the wrong reason.
    """
    scan = scan_sql_source(
        """
-- a cancel writes `error_code = CANCELLED_BY_USER` into the jobs row
error_code text,
""",
        "sample.sql",
    )
    assert scan.codes == set()

    scan = scan_typescript_source(
        """
///     finalised immediately: `cancelled`, `stop_reason = cancelled_by_user`,
///     `error_code = CANCELLED_BY_USER`, `finished_at` stamped.
const JOB_COLUMNS = "id,state,error_code,stop_reason,created_at";
""",
        "jobs/index.ts",
    )
    assert scan.codes == set()
    assert scan.stop_reasons == set()
    assert scan.unresolved == []