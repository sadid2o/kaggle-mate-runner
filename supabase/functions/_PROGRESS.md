# Edge Functions — progress log

Started 2026-09-26 by the second agent, after the first died mid-task with no
record. Everything below is what was actually done and actually run, separated
from what is still unverified. Nothing here is a claim that the code works —
read the "verification" section before trusting anything.

## State at handover (verified by reading the tree, not assumed)

- `accounts/`, `notebooks/`, `schedules/` each existed with a full handler and
  **no entry point** — `grep -rn "Deno.serve|export default"` over the whole
  functions tree returned nothing. Supabase requires one of the two, so all
  three deployed and served nothing.
- `config.toml` already declared `[functions.jobs]`, `[functions.alerts]`,
  `[functions.status]` with `verify_jwt = false`, and none of the three
  directories existed.
- `_shared/auth.ts` was referenced by `config.toml` but did not exist.
- `_shared/env.ts`'s `publishableKey()` doc referenced a `requirePublishableKey`
  that did not exist anywhere (grep: the only hits for both names are env.ts
  itself).

## Done in this session

1. **Entry points added** to `accounts/index.ts`, `notebooks/index.ts`,
   `schedules/index.ts`, plus every new function. Each is the same block:

   ```ts
   Deno.serve(async (request: Request) => {
     try {
       const { op, params } = await parseRequest(request);
       return await handleAccounts(op, params);
     } catch (error) {
       return errorResponse(error);
     }
   });
   ```

   The handler decomposition was left exactly as it was; only the entry was
   added (plus the imports it needs).

2. **Defect 2 — auth: chose "no credential check, and no `auth.ts`".**
   Reasoning: `_shared/db.ts` is right that `supabase_flutter`'s
   `FunctionsClient` attaches neither `apikey` nor `Authorization` to
   `functions.invoke` (its `_getAuthHeaders()` is applied to `auth`, `rest`,
   `storage` and `realtime`, not to `functions`). So a `requirePublishableKey`
   check would reject every real app request, and creating `_shared/auth.ts` to
   do that would have broken the app. Instead:
   - removed the stale claim from `config.toml` (line ~11) and replaced it with
     what is actually true — `verify_jwt = false` is required because the
     publishable key is not a JWT, there is no credential to inspect, and the
     real boundary is RLS plus the service key;
   - rewrote the `publishableKey()` doc comment in `_shared/env.ts` to say it is
     **never** proof of identity and that nothing in the functions calls it;
   - left `publishableKey()` itself **in place**: grep across the whole
     functions tree finds no caller, but removing an export is not required to
     fix the defect and something outside this tree (the runner reads
     `SUPABASE_PUBLISHABLE_KEY` from its own env) may reasonably want it. Say
     the word and it goes.

3. **Created `jobs/index.ts`** — ops `list`, `get`, `events`, `run_now`,
   `cancel`.
4. **Created `alerts/index.ts`** — ops `list`, `mark_read`, `mark_all_read`.
5. **Created `status/index.ts`** — op `health`, flat snake_case body (the one
   documented exception to the `itemResponse`/`itemsResponse` shapes).

`config.toml` needed no change beyond the comment: the directory names
`jobs`/`alerts`/`status` match the `[functions.*]` keys exactly (checked by
listing the tree).

No migration was needed. Every op is a plain select / insert / update through
the service key; nothing new had to be reachable from PostgREST.

## Decisions worth knowing (they are comments in the code too)

- **Job column names come straight from `migrations/0001_init.sql`**, including
  `job_state`'s nine values character for character (`pending, claimed, running,
  watching, stopped, completed, failed, cancelled, missed`). `"state"` on
  `jobs` is validated against that set here, because an unknown value would
  otherwise come back as a Postgres cast error (`invalid input syntax for type
  job_state`) instead of a sentence.
- **`run_now` writes `schedule_id = null`** on purpose: that is what makes it
  escape `jobs_one_per_schedule_instant` (unique partial index on
  `(schedule_id, planned_start_at) where schedule_id is not null`). Two manual
  runs of the same notebook are allowed; two scheduled runs of the same instant
  are not.
- **`run_now` leaves `kaggle_version_id` null**, exactly as `tick.py` does. The
  worker resolves the newest stored version itself at start time
  (`worker.py::start` does `order="version.desc", limit=1`). Filling it in here
  would be a claim about what ran that the runner can contradict.
- **The manual-run window is an invented constant**, and this is the one place a
  number had to be chosen that the brief does not supply:
  `MANUAL_RUN_MINUTES = 60` with `STOP_BUFFER_SECONDS = 300`. The app's
  confirmation sheet asks the user for nothing but "run it now" (checked:
  `notebook_detail_page.dart::_runNow` offers only confirm/cancel), so there is
  no user-chosen duration to read. 60 minutes is the app's own default for a new
  schedule, which makes it the least surprising value; 300s is the buffer
  `settings.stop_buffer_seconds` holds in 0001 and `schedule_engine` applies as
  `span + 300`. It is a constant at the top of `jobs/index.ts`, easy to change.
  If the intent is "run until it finishes", that is a different design and not
  what shipped.
- **`cancel` does not call `km_claim_job`.** Checked its `cancel` branch in
  0001: its WHERE is `state in ('pending','claimed','running')` — `watching` is
  **not** in it, and the app's job poll treats `watching` as live and offers
  Cancel for it (`jobs_controller.dart:86,128` test `j.isLive`, and
  `JobState.isLive` is `running || watching`). So the RPC would silently do
  nothing for a watching job. This function reproduces 0001's rule with that
  state included: `pending`/`claimed` are finalised immediately (`cancelled`,
  `stop_reason = cancelled_by_user`, `error_code = CANCELLED_BY_USER`,
  `finished_at`), anything else gets the flag only. Finalising a `pending` job
  matters most — `tick` claims pending rows in `planned_start_at` order, so a
  pending job left alone would be started and burn a real Kaggle push after the
  user cancelled it.
  Honest caveat: for a job in `claimed`, a worker may already be mid-push and
  `worker.py` writes `running` unconditionally afterwards
  (`worker.py:187`), briefly overwriting the `cancelled` state. The cancel flag
  is what actually stops the run (the notebook's watchdog polls it), so the run
  still ends cancelled.
- **`run_now` refuses an inactive notebook.** The Run-now button is not gated on
  `is_active` in `notebook_detail_page.dart`, but the switch means "do not run
  this" and `schedules/index.ts::assertNotebookRunnable` already refuses to
  schedule one — so starting it anyway would make the switch a lie. It is a 409
  with a sentence naming the fix. If the product wants a manual run to override
  the switch, this is the line to change.
- **`status`**: `failed_recently` is derived from `jobs` (state `failed`,
  `finished_at` within 24h) as instructed. Every failed job has a `finished_at`
  because `worker._finish` stamps it for every state in `FINAL_STATES`
  (`worker.py:57-61`), checked rather than assumed.
- **`last_tick_at` is the newest `jobs.created_at`** — the same value as
  `last_job_at`. That is not a compromise: it is literally what
  `lib/data/models/alert.dart`'s `lastTickAt` doc says it is ("The newest
  `jobs.created_at` ... doubles as a liveness signal"). **There is no true tick
  heartbeat to read**: `tick.py` writes nothing to `settings` (it only selects
  from it), writes no heartbeat row, and the only tables it writes are `jobs`
  and `job_events`. So the value cannot be sharper than "the last time any job
  row was created", and the comment in `status/index.ts` says so in place.

## GAP, still open — nothing writes `alerts`

Grepped `kaggle-mate-runner/runner/*.py`: **no file inserts into `alerts`.**
`tick.py` and `worker.py` write `jobs` and `job_events` only; `kaggle_cli.py`
has a comment saying an exception "carries stderr for the alert body" but no
caller ever writes one. So:

- `alerts` can read and mark rows, and will return an empty list until a writer
  exists.
- Nothing writes a tick heartbeat either (see above), so `last_tick_at` is
  derived rather than reported.
- I did **not** add a writer. It would mean deciding which failures deserve an
  alert row, which is a product decision, and a half-guessed writer is worse
  than the honest gap. The header comment in `alerts/index.ts` states the gap so
  the next person does not have to rediscover it.

## Verification actually run

Everything below was actually executed on this machine on 2026-09-26, with the
real output read back. At the time §1–§5 were written, Deno was **not installed**
and the Supabase CLI was **not installed** (confirmed with `which`), so nothing
had been executed or type-checked as Deno would. Deno was installed later the
same day and `deno check` was run for the first time — see §6, which supersedes
the type-checking caveats in §2 and in "Still unverified" below.

### 1. IMPORTANT — the brief's `node --check` gate does not work here

The brief says to use `node --check <file>.ts` as the syntax gate, having
"confirmed it passes on all 8 existing files". It does — and that is the problem:
**on Node v24.20.0, `node --check` exits 0 for essentially every `.ts` file,
valid or not.** It is not parsing these as TypeScript.

Proved with deliberate breakage of the real files (mutations confirmed applied
byte-for-byte, so this is not a no-op):

    P1  jobs/index.ts with its final `});` removed   -> node --check exit 0
    P2  intParam called with the wrong arity         -> node --check exit 0
    P3  a column name that does not exist            -> node --check exit 0
    P4  import from a module that does not exist     -> node --check exit 0

and on minimal files:

    unbalanced `{` in a .ts file    -> exit 0
    unbalanced `(` in a .ts file    -> exit 0
    stray `}` in a .ts file         -> exit 0
    a bare `@@@` token in a .ts file -> exit 0
    the same unbalanced brace in a .mjs -> exit 1 (correctly caught)

So `node --check` on `.ts` is a false-negative machine: it will report PASS on
genuinely broken TypeScript. **Do not use it as the gate.** `node --check`
passing is what the previous agent's eight files would have looked like too, so
it is not evidence of anything.

### 2. The gate that DOES work

`node:module`'s `stripTypeScriptTypes(src, { mode: "strip" })` is a real
TypeScript parser and it behaves correctly in both directions. Results:

    PASS  _shared/db.ts            (2189 bytes)
    PASS  _shared/env.ts           (3352 bytes)
    PASS  _shared/http.ts          (2881 bytes)
    PASS  _shared/kaggle.ts       (16685 bytes)
    PASS  _shared/request.ts       (7696 bytes)
    PASS  accounts/index.ts       (12602 bytes)
    PASS  notebooks/index.ts      (18009 bytes)
    PASS  schedules/index.ts      (13444 bytes)
    PASS  jobs/index.ts           (18105 bytes)
    PASS  alerts/index.ts          (6163 bytes)
    PASS  status/index.ts          (7099 bytes)

    11 passed, 0 failed

Control tests on the same gate, with mutation_applied confirmed for each:

    closing brace removed    -> gate CAUGHT it
    missing quote in const   -> gate CAUGHT it
    stray token              -> gate CAUGHT it
    bad import path          -> gate MISSED it   (expected)
    wrong column name        -> gate MISSED it   (expected)
    wrong intParam arity     -> gate MISSED it   (expected)

**What this gate does and does not prove.** It proves each file is
syntactically valid TypeScript that Node's TS parser accepts — no unbalanced
brackets, no bad tokens, no malformed declarations. It does **not** type-check.
The three "MISSED" cases above are exactly the classes of error it cannot see: a
wrong column name, a wrong import path and a wrong argument count all pass it.
It is a strictly better syntax gate than the brief's, and still strictly weaker
than `deno check` — which had never been run at the time this was written, and
has since been (see §6), where it immediately found six errors this gate had
passed.

### 3. Column names, verified mechanically against the schema

Parsed `0001_init.sql` into a real table -> column map and checked every column
string emitted by every function against it. Result: **no unknown column name in
any file.** Specifically:

- `JOB_COLUMNS` resolves to all 18 columns of `jobs`, and the set is identical
  to the schema's 18 — nothing missing, nothing invented. (The literal was split
  across three `+`-joined lines, so this needed the concatenation evaluated, not
  just grepped — the first pass checked each fragment separately and was
  misleading. That split turned out to be a real bug, not just a reading
  hazard: see §6. It is now one line.)
- `ALERT_COLUMNS` is all 7 columns of `alerts`.
- Every `.eq/.order/.select/.is/.gte/.in` column reference across all six
  functions is a real column.
- The nine `job_state` values in `jobs/index.ts` are **character-for-character
  identical** to the enum in the migration (compared programmatically, not by
  eye), and `FINAL_STATES` matches `worker.py`'s `FINAL_STATES` exactly
  (`stopped, completed, failed, cancelled, missed`).
- Every `level` the runner writes to `job_events` (`info`, `warn`, `error`) is
  inside the column's `('debug','info','warn','error')` CHECK, and every
  `severity` is inside `alerts`' `('info','warn','error')`.

### 4. Op contract, verified mechanically against the app

Extracted every `_api.invoke` call from `app_repository.dart` and compared:

    jobs     op=list(limit,notebook_id,state) get(id) events(job_id,limit)
             run_now(notebook_id) cancel(id)
    alerts   op=list(limit) mark_read(id) mark_all_read()
    status   op=health()

The dispatch `switch` blocks in the three new functions contain exactly these
ops and no others. Note `mark_all_read` is called with a body of `{op: ...}`
only, so that branch must not require a parameter — it takes `_params` and reads
nothing. `mark_read` reads `id` with `intParam`, not `uuidParam`, because
`alerts.id` is `bigserial`.

### 5. Entry points and config, verified

- Exactly **one** real `Deno.serve(...)` per function directory, and each calls
  its own handler (`handleAccounts`, `handleNotebooks`, `handleSchedules`,
  `handleJobs`, `handleAlerts`, `handleStatus`). (Each file also contains the
  word `Deno.serve` inside its explanatory comment; a naive count says 2. The
  code/comment split was checked line by line.)
- Directory names and `config.toml`'s `[functions.*]` keys are the **same set**,
  in the same order: `accounts, alerts, jobs, notebooks, schedules, status`.
- `verify_jwt = false` is present under all six keys (checked line-based).
- Response shapes: `jobs`, `alerts`, `accounts`, `notebooks`, `schedules` use
  `itemResponse`/`itemsResponse`/`okResponse`; **`status` is the only file with a
  bare `Response.json`** and no `itemResponse`, which is the documented
  exception the app's `runnerStatus` requires.
- Every user-facing error string in all six functions is **English**; checked
  programmatically with a Python codepoint scan (not `grep -P`, which this Git
  Bash breaks on for the Bengali ranges), 0 Bengali characters in the six
  functions or in `_shared/`.

  This line previously claimed the opposite — that all 89 messages contained
  Bengali and 0 did not. That was wrong when it was written and is wrong now.
  The measurement was redone on 2026-09-26 across the runner repo, `lib/` and
  `test/` together: 0 Bengali characters in any code file. The only place
  Bengali survives is `kaggle_mate/docs/*` (11,582 characters across 6 files),
  which the app never loads at runtime. The requirement is English everywhere,
  so docs are the remaining cleanup, not the code.

### 6. Finally type-checked — `deno check`, run for the first time

Deno 2.9.7 was installed later on 2026-09-26 (`x86_64-pc-windows-msvc`, v8
15.0.245.2, TypeScript 6.0.3) and `deno check` was run over the tree for the
first time in this project's life. It found **six real errors in two files** that
every earlier gate had passed — including the `stripTypeScriptTypes` parser in §2
and the whole-tree column audit in §3.

Run against the broken forms on purpose (both mutations confirmed applied
byte-for-byte first, and the venv Python had to be used by absolute path because
`python` is not on PATH here):

    jobs/index.ts       TS2352 x4  `as Record<string, unknown>` cast of GenericStringError
                        TS2339 x1  found.data.state
    schedules/index.ts  TS2554 x1  Expected 3-4 arguments, but got 5
                                    -> Found 6 errors.

**The cause of the five `jobs` errors was `JOB_COLUMNS`.** Splitting that string
literal across `+`-joined lines widens its type from the literal to plain
`string`, and `postgrest-js` infers its result type by *parsing that literal at
compile time*. So it is not cosmetic: the client falls back to its error type,
every row becomes `GenericStringError`, and `.select(...)` on any other column
stops compiling. Fixed by putting the literal on one line, with a comment saying
it must stay there. The other functions were only unaffected because their
literals happen to be short enough to fit on one line — which is luck, not
design, and is worth knowing before anyone reformats one.

**The one `schedules` error was a real behavioural bug, not a typing nit.**
`buildRow` takes 4 parameters and line 155 called it with 5, a leftover `null`
from an older signature. That shifted every later argument one slot early, so
`partial` received the `null` — falsy — and partial mode silently turned itself
off. `setScheduleActive` sends nothing but `is_active`, so the schedule
enable/disable toggle in the app was demanding all eight schedule fields and
failing. The type checker caught the arity; the behaviour it was hiding is why
this is documented in the code rather than quietly deleted.

After both fixes, all six entry points: **EXIT=0**.

What `deno check` does and does not catch, measured by planting each fault in a
copy and running it (this corrects the claim in §2 and in my earlier notes,
which said a wrong import path is missed — it is not):

    wrong import path   -> CAUGHT   (TS2307)
    wrong column name   -> MISSED   (a string is just a string)

The missed case is the gap the tests below exist to fill. `deno.lock` is now
committed, so CI resolves exactly the dependency versions this was checked
against, and the workflow runs `--frozen` so a silent dependency change fails
loudly instead of changing what "green" means.

### 7. Unit tests over the shared modules

`tests/shared_test.ts` — 42 tests over `_shared/request.ts` and `_shared/http.ts`,
chosen first because they are pure (no network, no Supabase, no `Deno.env`) and
because all six functions' inputs and outputs pass through them. `deno test`:
**42 passed, 0 failed.**

Control-tested the suite the same way: breaking `intParam`'s clamping, breaking
`daysParam`'s dedupe/sort, and inverting the body-over-query precedence rule were
each caught, and 42/42 passed again after restoration. Two behaviours are pinned
deliberately rather than "fixed": `intParam` CLAMPS out-of-range rather than
throwing, and `optionalDate` checks date *shape* but not calendar validity, so
`2026-99-99` passes through to Postgres.

Not yet covered: `_shared/kaggle.ts` (401 lines) and the six handlers themselves,
which need a mocked Supabase client.

### 8. The failure-code contract, and the bug that was hiding in it

The runner's failure codes are read by the Flutter app. Nothing links the two:
the codes are strings written in Python, TypeScript and SQL, and read in Dart.
A string is not a type in either language, so no compiler, analyzer or linter
can notice when the two sides disagree — and they had disagreed **completely**.

Read out of the sources side by side, the overlap was **zero**:

    app explained            runner writes
    -----------------------  -----------------------------
    kaggle_auth              AUTH_FAILED
    kaggle_push_failed       PUSH_FAILED, QUOTA_LIMIT
    timeout                  KAGGLE_RUN_ERROR, WORKER_CRASH
    notebook_never_started   MISSED_WINDOW
    runner_error             DEADLINE_STOP_UNCONFIRMED
                             CANCELLED_BY_USER
                             OUTPUT_FETCH_FAILED  (job_events only)
                             CANCEL_UNCONFIRMED   (job_events only)

Not one string appears on both sides. So **every** failure a user could hit fell
through to the generic "The log page will show the full reason", while the five
strings the app did translate were dead — nothing has ever written any of them.
The same was true of `stop_reason`: the app labelled `cancelled` and `watchdog`,
neither of which the runner writes, and passed `cancelled_by_user` and
`notebook_finished` through raw.

Three separate defects came out of reading the code rather than guessing:

1. **Every real failure was unexplained** (above).
2. **Cancelling a run looked like a failure.** The run screen showed its card
   whenever `error_code` was non-null — and cancelling a *pending* run writes
   `CANCELLED_BY_USER`. A user who deliberately stopped a run was told it failed.
   `OUTPUT_FETCH_FAILED` had the same problem in reverse: the run succeeded, and
   only its saved log is missing, so a red failure card is wrong.
3. **The `alerts` table has no writer.** Grepping `runner/` for it returns
   nothing, so the Alerts page, its unread badge and its notifications can only
   ever be empty. Still open.

The fix is a single vocabulary, `lib/core/error/runner_failure.dart` in the app,
with one entry per code giving a title, a severity and a plain explanation — and
`tools/scan_error_codes.py` here, which reads the runner's Python (via `ast`),
TypeScript and SQL every run and derives what it can write. It **discovers**
which helpers write `jobs` and which write `job_events` from their bodies, so
the run-level / event-only split is checked rather than trusted; it follows
`error_code=code_tag` back to its assignment, because `QUOTA_LIMIT` and
`PUSH_FAILED` never appear at the write site; and it resolves
`stop_reason=job.get("stop_reason_pref")` against the migration's CHECK
constraint, so the migration is part of the contract instead of a comment about
it. Anything it cannot read is reported and fails the test — an unreadable write
is exactly the shape of the bug it exists to catch.

`tests/test_error_codes.py` — 24 tests, **all passing**, comparing the two
vocabularies in *both* directions: a code the runner writes and the app cannot
explain, and a code the app explains that nothing writes. The second half is the
one that hid the first, because the fallback looked like it was working.

Control-tested by planting seven faults one at a time — a new unexplained code,
a dead code in the app, a code moved to the wrong half, a stale snapshot, an
unlabelled stop reason, an unreadable write, and a code deleted from the app's
list. **All seven were caught**, and the suite returned to green after each
restore. (The first harness was itself broken: `Path.write_text` rewrites LF as
CRLF on Windows, so its "revert" changed the file and a hash assertion failed for
a reason unrelated to the mutation. That in turn exposed a real defect — one code
path hashed normalised text while another hashed raw bytes, so they disagreed by
construction on any CRLF checkout. Both now use `dart_file_hash`, which
normalises line endings before hashing, and there is a test for exactly that.)

The app's vocabulary is committed here as `contract/runner_failure.json`, since
this job cannot see the Dart file. The full comparison runs wherever both
checkouts are present; in this job the codes are checked against the snapshot
plus a frozen list. Regenerate with
`python tools/scan_error_codes.py --write-contract`.

Runner suite: **83 → 107 passed**. App suite: **75 → 97 passed**, with
`test/core/runner_failure_test.dart` covering what the scanner cannot see — that
a user's own cancel is not described as a failure, and that an unknown code is
shown rather than swallowed.

## Still unverified — do not treat as working

- **Nothing was deployed or executed.** No function has ever served a request.
- **Type checking is verified going forward (see §6), but nothing downstream of
  it is.** All six entry points are clean under `deno check` 2.9.7 and 42 unit
  tests pass. That says the code is internally consistent and that the shared
  modules behave as tested; it says nothing about whether Postgres accepts the
  queries, because a wrong column name is invisible to the checker (§6).
- No PostgREST call was made, so no query is known to be accepted by the server.
  In particular, `head: true` counts and `.maybeSingle()` on an ordered-limit-1
  query are used per the documented behaviour of the client; they are unexercised.
- `job_events.payload` and `alerts.body` are `jsonb`/`text`; the app reads them
  as a Map / String?. Untested.
- **CORS was not addressed.** The app is Flutter/Android, where it is not
  needed. A Flutter *web* build calling these functions would need
  `Access-Control-Allow-*` headers, which no function sends.
- Whether the app's job list actually renders notebook names and account labels
  end-to-end is unverified — both the flat and the embedded shapes
  `Job.fromJson` accepts are sent, but nothing has parsed them for real.
- The `run_now` window constant (`MANUAL_RUN_MINUTES = 60`) is a judgement call,
  not a verified requirement. See the decision note above.
- **The failure-code contract is checked, but nothing has exercised it against a
  live database.** The guard proves the two vocabularies agree with the sources
  as written; it cannot prove that Postgres accepts those writes, because no
  migration has been applied. See the note about migrations below.
- **The `alerts` table still has no writer** (§8, defect 3). The Alerts page
  renders correctly and can only ever be empty until something inserts into it.