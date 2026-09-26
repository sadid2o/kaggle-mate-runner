"""A machine-readable inventory of every failure code and stop reason the runner
can write — read out of the source, not maintained by hand.

**Why a scanner and not a list.** These codes are a contract between two
languages that cannot check each other. The runner writes them in Python,
TypeScript and SQL; the app reads them in Dart. No toolchain links the two, and
they drifted apart completely once already: the app translated five codes
(`kaggle_auth`, `kaggle_push_failed`, `timeout`, `notebook_never_started`,
`runner_error`) **none of which the runner has ever written**, while all ten it
does write fell through to a generic message. A hand-kept list would have been
just as stale; this reads the sources every run.

**What it derives rather than assumes:**

* Which local functions write the `jobs` table and which write `job_events` is
  discovered from their bodies. `_finish` is not special-cased — it is simply
  the function whose body calls `update("jobs", ...)`. If the helpers are
  renamed or split, the classification follows.
* `error_code=code_tag` is resolved by following `code_tag` back to its
  assignment, which is the only way to see `QUOTA_LIMIT` and `PUSH_FAILED`:
  neither literal appears at the write site.
* `stop_reason=job.get("stop_reason_pref")` is resolved to the values the
  migration's CHECK constraint permits, so the migration is part of the
  contract instead of a comment about it.

Anything it cannot resolve is reported in `Scan.unresolved` rather than dropped
— an unreadable write is the exact shape of bug this is here to catch, so it
must fail loudly rather than quietly contribute nothing.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Where the Flutter app lives. The two checkouts are siblings on this machine;
#: `KM_APP_ROOT` overrides it for any other layout.
DEFAULT_APP_ROOT = REPO_ROOT.parent / "kaggle_mate"

#: The vocabulary snapshot, committed so the runner's CI — which can only see
#: this repository — can still check its own side of the contract.
CONTRACT_PATH = REPO_ROOT / "contract" / "runner_failure.json"

#: Relative to the app root, so the snapshot records a portable path.
DART_VOCABULARY_PATH = "lib/core/error/runner_failure.dart"

#: Code-shaped: SCREAMING_SNAKE with at least one underscore. Deliberately not
#: "any uppercase string" — that would drag in `SUPABASE_URL` and every other
#: environment variable name, which are not codes.
CODE_LITERAL = re.compile(r"^[A-Z][A-Z0-9]*_[A-Z0-9_]+$")


@dataclass
class Scan:
    """What one pass over a source tree found."""

    #: Codes written to the `jobs` row, i.e. reachable as `jobs.error_code`.
    job_row_codes: set[str] = field(default_factory=set)
    #: Codes written to `job_events` only.
    event_codes: set[str] = field(default_factory=set)
    #: Values written to `jobs.stop_reason`.
    stop_reasons: set[str] = field(default_factory=set)
    #: The values `stop_reason_pref`'s CHECK constraint allows, read from the
    #: migration. The worker copies one of these into `stop_reason`.
    stop_reason_pref_values: set[str] = field(default_factory=set)
    #: Writes this scanner could not read to a literal, as `path:line — detail`.
    unresolved: list[str] = field(default_factory=list)

    @property
    def codes(self) -> set[str]:
        return self.job_row_codes | self.event_codes

    def merge(self, other: Scan) -> None:
        self.job_row_codes |= other.job_row_codes
        self.event_codes |= other.event_codes
        self.stop_reasons |= other.stop_reasons
        self.stop_reason_pref_values |= other.stop_reason_pref_values
        self.unresolved.extend(other.unresolved)


@dataclass
class DartVocabulary:
    """The app's declared vocabulary, parsed out of `runner_failure.dart`."""

    codes: set[str]
    run_level_codes: set[str]
    event_only_codes: set[str]
    stop_reasons: set[str]
    #: Code value -> `failure`, `cancelled` or `notice`.
    severities: dict[str, str]
    sha256: str

    def as_contract(self) -> dict:
        return {
            "source": DART_VOCABULARY_PATH,
            "sha256": self.sha256,
            "codes": sorted(self.codes),
            "run_level_codes": sorted(self.run_level_codes),
            "event_only_codes": sorted(self.event_only_codes),
            "stop_reasons": sorted(self.stop_reasons),
            "severities": dict(sorted(self.severities.items())),
        }


# --------------------------------------------------------------- python


def _string_constants(node: ast.AST) -> set[str]:
    """Every string constant anywhere inside `node`."""
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _table_writer_roles(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names of the local functions that write `jobs` and `job_events`.

    Discovered from bodies, so `_finish`/`_event` are incidental. A call is a
    write when it is `<something>.update("jobs", ...)` or
    `<something>.insert("job_events", ...)`.
    """
    job_writers: set[str] = set()
    event_writers: set[str] = set()

    functions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for fn in functions:
        for call in ast.walk(fn):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            func = call.func
            if not isinstance(func, ast.Attribute):
                continue
            first = call.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            if func.attr == "update" and first.value == "jobs":
                job_writers.add(fn.name)
            elif func.attr in {"insert", "upsert"} and first.value == "job_events":
                event_writers.add(fn.name)

    return job_writers, event_writers


def _write_role(
    call: ast.Call,
    job_writers: set[str],
    event_writers: set[str],
) -> str | None:
    """`"job"`, `"event"`, or `None` if this call is not a recognised write."""
    func = call.func
    if isinstance(func, ast.Name):
        if func.id in job_writers:
            return "job"
        if func.id in event_writers:
            return "event"
        return None

    # A direct `db.update("jobs", ...)` with the code in an inline dict.
    if isinstance(func, ast.Attribute) and call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            if func.attr == "update" and first.value == "jobs":
                return "job"
            if func.attr in {"insert", "upsert"} and first.value == "job_events":
                return "event"
    return None


def _resolve_error_code(
    node: ast.AST, name_literals: dict[str, set[str]]
) -> set[str] | None:
    """The literal codes a write site can produce, or `None` if unreadable."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return name_literals.get(node.id)
    return None


def _resolve_stop_reason(
    node: ast.AST,
    name_literals: dict[str, set[str]],
    pref_values: set[str],
) -> set[str] | None:
    """The values a `stop_reason=` write can produce, or `None` if unreadable.

    `job.get("stop_reason_pref")` yields whichever of the migration's permitted
    prefs is stored, so the migration's CHECK is what bounds it. The `or "x"`
    form that supplies a default is followed too, because that default is a
    value the column can really hold.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return name_literals.get(node.id)
    if isinstance(node, ast.BoolOp):
        values: set[str] = set()
        for operand in node.values:
            resolved = _resolve_stop_reason(operand, name_literals, pref_values)
            if resolved is None:
                return None
            values |= resolved
        return values
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get" and node.args:
            key = node.args[0]
            if isinstance(key, ast.Constant) and key.value == "stop_reason_pref":
                return set(pref_values)
    return None


def scan_python_source(
    text: str, filename: str, pref_values: set[str] | None = None
) -> Scan:
    """Scan one Python module's source for the codes and reasons it writes."""
    scan = Scan()
    pref_values = pref_values or set()
    tree = ast.parse(text, filename=filename)

    job_writers, event_writers = _table_writer_roles(tree)

    name_literals: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.targets:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                name_literals[target.id] = _string_constants(node.value)

    for call in [n for n in ast.walk(tree) if isinstance(n, ast.Call)]:
        role = _write_role(call, job_writers, event_writers)

        # `error_code=` / `stop_reason=` keyword forms.
        for kw in call.keywords:
            if kw.arg == "error_code":
                codes = _resolve_error_code(kw.value, name_literals)
                if codes is None:
                    scan.unresolved.append(
                        f"{filename}:{call.lineno} — error_code= could not be read"
                    )
                elif role == "job":
                    scan.job_row_codes |= codes
                elif role == "event":
                    scan.event_codes |= codes
                else:
                    scan.unresolved.append(
                        f"{filename}:{call.lineno} — error_code written by "
                        f"{_callee_name(call)} which writes neither table"
                    )
            elif kw.arg == "stop_reason":
                reasons = _resolve_stop_reason(kw.value, name_literals, pref_values)
                if reasons is None:
                    scan.unresolved.append(
                        f"{filename}:{call.lineno} — stop_reason= could not be read"
                    )
                else:
                    scan.stop_reasons |= reasons

        # Inline-dict form: `db.update("jobs", {...}, {"error_code": "X"})`.
        for arg in call.args:
            if not isinstance(arg, ast.Dict):
                continue
            for key, value in zip(arg.keys, arg.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    continue
                if key.value == "error_code":
                    codes = _resolve_error_code(value, name_literals)
                    if codes is None:
                        scan.unresolved.append(
                            f"{filename}:{call.lineno} — dict error_code unreadable"
                        )
                    elif role == "job":
                        scan.job_row_codes |= codes
                    elif role == "event":
                        scan.event_codes |= codes
                    else:
                        scan.unresolved.append(
                            f"{filename}:{call.lineno} — dict error_code written by "
                            f"{_callee_name(call)} which writes neither table"
                        )
                elif key.value == "stop_reason":
                    reasons = _resolve_stop_reason(value, name_literals, pref_values)
                    if reasons is None:
                        scan.unresolved.append(
                            f"{filename}:{call.lineno} — dict stop_reason unreadable"
                        )
                    else:
                        scan.stop_reasons |= reasons

    return scan


def _callee_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return "<expression>"


def scan_python_dir(root: Path, pref_values: set[str]) -> Scan:
    scan = Scan(stop_reason_pref_values=set(pref_values))
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        scan.merge(
            scan_python_source(
                path.read_text(encoding="utf-8"), path.name, pref_values
            )
        )
    return scan


# ----------------------------------------------------------- typescript


def scan_typescript_source(text: str, filename: str) -> Scan:
    """Scan one TypeScript module for job-row / job-event writes.

    Line-based rather than parsed: the shapes are narrow — an object key
    (`error_code: "X"`) and a PostgREST select list (a comma-joined column
    string). Any other mention of `error_code` on a live line is reported as
    unresolved, so a write this cannot read fails the guard instead of passing
    it.
    """
    scan = Scan()

    # `stop_reason_pref` is a different column — the *preference* chosen in the
    # schedule form, not the recorded reason. It contains `stop_reason` as a
    # prefix, so it is blanked out before scanning; otherwise every validation
    # line about it reads as an unreadable `stop_reason` write.
    text = text.replace("stop_reason_pref", "stopPref")

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if "error_code" not in line and "stop_reason" not in line:
            continue

        matched = False
        for key in ("error_code", "stop_reason"):
            pattern = re.compile(rf"""\b{key}\s*:\s*['"]([^'"]+)['"]""")
            for hit in pattern.finditer(line):
                if key == "error_code":
                    scan.job_row_codes.add(hit.group(1))
                else:
                    scan.stop_reasons.add(hit.group(1))
                matched = True
        if matched:
            continue

        # A PostgREST select list: one long comma-joined column string.
        if "," in line:
            continue

        scan.unresolved.append(f"{filename}:{number} — {line[:90]}")
    return scan


def scan_typescript_dir(root: Path) -> Scan:
    scan = Scan()
    for path in sorted(root.rglob("*.ts")):
        if "tests" in path.parts or "node_modules" in path.parts:
            continue
        scan.merge(
            scan_typescript_source(
                path.read_text(encoding="utf-8"), path.name
            )
        )
    return scan


# ------------------------------------------------------------------ sql


def scan_sql_source(text: str, filename: str) -> Scan:
    """Scan one migration for the codes, reasons and prefs it writes.

    Exhaustive on purpose: **every** code-shaped literal in live SQL is treated
    as vocabulary. The two migrations hold exactly two (`CANCELLED_BY_USER`, and
    the `CANCELLED_BY_USER` written when a pending job is cancelled) and one
    pref list, so an unrelated SCREAMING_SNAKE literal appearing later is a
    decision someone should make consciously rather than something to ignore.
    """
    scan = Scan()
    lines = text.splitlines()

    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("--"):
            continue

        for hit in re.finditer(r"'([^']+)'", line):
            value = hit.group(1)
            if CODE_LITERAL.match(value):
                # Placement reflects where the SQL puts it. `error_code = case
                # ... then 'X'` sets the job row; a marker in a comment or a
                # constraint name is not a code and does not match anyway.
                scan.job_row_codes.add(value)

        # `check (stop_reason_pref in ('duration', 'clock_time', 'manual'))`
        if "stop_reason_pref" in line and " in (" in line:
            inside = line.split(" in (", 1)[1]
            scan.stop_reason_pref_values |= set(re.findall(r"'([^']+)'", inside))

    # `stop_reason = case ... then 'x' ...` — the branches only.
    for number, raw in enumerate(lines):
        if not re.match(r"^stop_reason\s*=\s*case", raw.strip()):
            continue
        for following in lines[number + 1:]:
            stripped = following.strip()
            if re.match(r"^\w+\s*=", stripped) and not stripped.startswith("stop_reason"):
                break
            for hit in re.finditer(r"\bthen\s+'([^']+)'", stripped):
                scan.stop_reasons.add(hit.group(1))

    return scan


def scan_sql_dir(root: Path) -> Scan:
    scan = Scan()
    for path in sorted(root.glob("*.sql")):
        scan.merge(scan_sql_source(path.read_text(encoding="utf-8"), path.name))
    return scan


# ----------------------------------------------------------------- dart


def _dart_list(text: str, name: str, constants: dict[str, str]) -> set[str]:
    """The contents of `static const <name> = <String>[...]`.

    Entries are either a string literal or a reference to one of the file's own
    constants; a reference that does not resolve is a hard error, because a
    typo'd constant would otherwise silently shrink the vocabulary.
    """
    match = re.search(rf"static const {name} = <String>\[(.*?)\];", text, re.S)
    if match is None:
        raise ValueError(f"no `static const {name} = <String>[...]` in the Dart file")

    values: set[str] = set()
    for literal, identifier in re.findall(
        r"'([^']*)'|([A-Za-z_]\w*)", match.group(1)
    ):
        if literal:
            values.add(literal)
        elif identifier:
            if identifier not in constants:
                raise ValueError(
                    f"{name} references `{identifier}`, which is not a constant "
                    f"in the same file"
                )
            values.add(constants[identifier])
    return values


def dart_file_hash(path: Path) -> str:
    """SHA-256 of the file, independent of its line endings.

    Line endings are normalised before hashing **on purpose**. The two checkouts
    live on different machines, and `core.autocrlf` decides whether a checkout
    gets CRLF or LF — so a byte-exact hash would fail on a Windows clone of a
    file nobody had edited, which is a false alarm that teaches people to ignore
    the check. The hash is here to notice *edits*, and an edit that only changed
    line endings is not one.

    Both this and the test that recomputes the hash must use this helper; when
    they used different readings (normalised text in one, raw bytes in the other)
    they disagreed by construction on any CRLF checkout, which is exactly the
    false alarm described above.
    """
    raw = path.read_bytes()
    return hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()


def parse_dart_vocabulary(app_root: Path) -> DartVocabulary:
    """Parse the app's declared vocabulary and hash the file it came from."""
    path = app_root / DART_VOCABULARY_PATH
    text = path.read_text(encoding="utf-8")

    constants = {
        name: value
        for name, value in re.findall(r"static const (\w+) = '([^']+)';", text)
    }

    severities: dict[str, str] = {}
    map_match = re.search(
        r"static const Map<String, _Meaning> _byCode = \{(.*?)\n  \};", text, re.S
    )
    if map_match is None:
        raise ValueError("no `_byCode` map in the Dart file")
    current: str | None = None
    for line in map_match.group(1).splitlines():
        entry = re.match(r"\s*(\w+): _Meaning\(", line)
        if entry:
            current = entry.group(1)
        severity = re.search(r"severity: FailureSeverity\.(\w+)", line)
        if severity and current:
            severities[current] = severity.group(1)

    codes = _dart_list(text, "codes", constants)

    # Re-key severities onto the code values, so the snapshot — and the runner's
    # CI, which never sees this file — can check how a code is treated and not
    # just that it is known.
    by_value = {
        constants[name]: severity
        for name, severity in severities.items()
        if name in constants
    }

    return DartVocabulary(
        codes=codes,
        run_level_codes=_dart_list(text, "runLevelCodes", constants),
        event_only_codes=_dart_list(text, "eventOnlyCodes", constants),
        stop_reasons=_dart_list(text, "stopReasons", constants),
        severities=by_value,
        sha256=dart_file_hash(path),
    )


# ------------------------------------------------------------------ api


def scan_repo(root: Path = REPO_ROOT) -> Scan:
    """Everything the runner can write, across Python, TypeScript and SQL."""
    sql = scan_sql_dir(root / "supabase" / "migrations")
    python = scan_python_dir(root / "runner", sql.stop_reason_pref_values)
    typescript = scan_typescript_dir(root / "supabase" / "functions")

    scan = Scan()
    for part in (sql, python, typescript):
        scan.merge(part)
    return scan


def load_contract(path: Path = CONTRACT_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_contract(app_root: Path, path: Path = CONTRACT_PATH) -> dict:
    """Regenerate the committed snapshot from the Dart file."""
    contract = parse_dart_vocabulary(app_root).as_contract()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    return contract


def _main() -> int:
    import os
    import sys

    app_root = Path(os.environ.get("KM_APP_ROOT", DEFAULT_APP_ROOT))

    if "--write-contract" in sys.argv:
        contract = write_contract(app_root)
        print(f"wrote {CONTRACT_PATH.relative_to(REPO_ROOT)}")
        print(f"  {len(contract['codes'])} codes, sha256 {contract['sha256'][:12]}…")
        return 0

    scan = scan_repo()
    print("jobs.error_code  :", ", ".join(sorted(scan.job_row_codes)))
    print("job_events only  :", ", ".join(sorted(scan.event_codes)))
    print("stop_reason      :", ", ".join(sorted(scan.stop_reasons)))
    print("pref values      :", ", ".join(sorted(scan.stop_reason_pref_values)))
    if scan.unresolved:
        print("UNRESOLVED:")
        for item in scan.unresolved:
            print("  -", item)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())