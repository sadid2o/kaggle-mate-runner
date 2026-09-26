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
real output read back. Deno is **not installed** and the Supabase CLI is **not
installed** (confirmed with `which`), so nothing was executed or type-checked as
Deno would.

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
than `deno check`, which has never been run.

### 3. Column names, verified mechanically against the schema

Parsed `0001_init.sql` into a real table -> column map and checked every column
string emitted by every function against it. Result: **no unknown column name in
any file.** Specifically:

- `JOB_COLUMNS` resolves to all 18 columns of `jobs`, and the set is identical
  to the schema's 18 — nothing missing, nothing invented. (The literal is split
  across three `+`-joined lines, so this needed the concatenation evaluated, not
  just grepped — the first pass checked each fragment separately and was
  misleading.)
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
- Every user-facing error string in all six functions contains Bengali; checked
  programmatically, 0 messages without it (89 messages across the six files).

## Still unverified — do not treat as working

- **Nothing was deployed or executed.** No function has ever served a request.
- **Type checking is unverified.** `deno check` has never run on this tree, and
  Deno is not installed. The gate in §2 is a parser, not a type checker — see the
  three "MISSED" controls.
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