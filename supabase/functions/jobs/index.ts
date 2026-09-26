// `jobs` — the run history, and the two things the user can do to it.
//
// The app is read-only about job state: `worker.py` advances a job through its
// lifecycle, and nothing here ever moves a job forward on its own. The two
// writes this function performs are the two the user asks for explicitly, and
// both are deferrals rather than actions:
//
//   * `run_now` writes a `pending` row and stops. It does **not** start
//     anything: the app holds no credential that could start a Kaggle run, so
//     the runner picks the row up on its next tick (within 5 minutes). The cost
//     is that latency, and the benefit is that a stolen phone cannot spend the
//     user's GPU quota. The confirmation sheet in `notebook_detail_page.dart`
//     says the 5 minutes out loud so the wait is explained rather than
//     mysterious.
//   * `cancel` sets `cancel_requested`, which the notebook's injected watchdog
//     polls. Kaggle exposes no supported "stop this kernel" endpoint (the
//     reasoning is written out in `migrations/0002_cancel_channel.sql`), so a
//     cancel is *cooperative* and lands on the watchdog's next 30-second check.
//
// Read `_shared/db.ts` before changing anything about authorization: no
// credential arrives from the app, so there is none to check. The boundary is
// that every query below runs with the service key, which never leaves the
// server.

import { admin } from "../_shared/db.ts";
import {
  badRequest,
  conflict,
  errorResponse,
  itemResponse,
  itemsResponse,
  notFound,
  upstream,
} from "../_shared/http.ts";
import {
  intParam,
  optionalString,
  type Params,
  parseRequest,
  uuidParam,
} from "../_shared/request.ts";

/// Every column of `jobs`, listed rather than `*` so a column added later does
/// not silently enter a response the app has not been taught to read.
/// `Job.fromJson` ignores the ones it does not use.
/// Every column of `jobs`, and it **must stay on one line**.
///
/// Splitting this literal across `+`-joined lines widens its type from the
/// literal to `string`, and `postgrest-js` infers its result type by parsing
/// that literal at compile time. A widened `string` is therefore not a
/// cosmetic difference: the client falls back to its error type for the whole
/// query, every row becomes `GenericStringError`, and `.select(...)` on any
/// other column stops compiling. `deno check` caught exactly that here; the
/// other functions were fine only because their literals happen to be short
/// enough to fit on one line.
const JOB_COLUMNS = "id,schedule_id,notebook_id,account_id,kaggle_version_id,state,planned_start_at,planned_stop_at,started_at,finished_at,timeout_seconds,stop_reason_pref,stop_reason,error_code,last_status,log_path,cancel_requested,created_at";

/// The `job_state` enum, character for character as `migrations/0001_init.sql`
/// declares it. This is not decoration. The app sends a state back in a filter
/// (the run list's filter chips), and Postgres answers an unknown value with
/// `invalid input syntax for type job_state` -- a 500 the user cannot act on.
/// Checking here turns that into a sentence naming what is allowed.
///
/// Note the three values that deliberately do NOT exist: there is no `queued`,
/// no `dispatched` and no `stopping`. A job waiting for a worker is `pending`,
/// one a worker has taken is `claimed`, and a cancel in flight is the boolean
/// `cancel_requested`, not a state.
const JOB_STATES = [
  "pending",
  "claimed",
  "running",
  "watching",
  "stopped",
  "completed",
  "failed",
  "cancelled",
  "missed",
] as const;

/// The states from which nothing further will happen. Kept in step with
/// `worker.py`'s `FINAL_STATES` and with `JobState.isFinal` on the app side.
const FINAL_STATES = ["stopped", "completed", "failed", "cancelled", "missed"];

/// How long a manual run is allowed to live.
///
/// This is the one number the brief could not supply and the one place a real
/// choice had to be made. A scheduled job's window comes from its schedule
/// (`duration_minutes`, or `stop_at_time` -- whichever is earlier, resolved by
/// `schedule_engine._resolve_stop`). A manual "Run now" has no schedule behind
/// it and the confirmation sheet asks the user for nothing, so there is no
/// user-chosen duration to read. 60 minutes is the same default the app puts in
/// a brand-new schedule form, which makes it the least surprising answer; the
/// runner enforces it through `planned_stop_at`, which its injected watchdog
/// reads as the hard deadline.
const MANUAL_RUN_MINUTES = 60;

/// The margin between our deadline and Kaggle's own session limit, so Kaggle is
/// the backstop and never the thing being aimed at. Same value and same
/// reasoning as the `stop_buffer_seconds` setting in 0001 and
/// `DueJob.timeout_seconds = span + 300` in `schedule_engine.py`.
const STOP_BUFFER_SECONDS = 300;

export async function handleJobs(op: string, params: Params): Promise<Response> {
  const db = admin();

  switch (op) {
    case "list":
      return listJobs(db, params);
    case "get":
      return getJob(db, params);
    case "events":
      return listEvents(db, params);
    case "run_now":
      return runNow(db, params);
    case "cancel":
      return cancelJob(db, params);
    default:
      throw badRequest(
        `"${op}" is not a valid operation. jobs supports: list, get, events, run_now, cancel.`,
      );
  }
}

/// The run list.
///
/// Ordered by `planned_start_at` descending, not by `created_at`. The two differ
/// for a scheduled job -- the row is written by `tick` up to a tick before the
/// run is due -- and `planned_start_at` is the run's own timestamp, which is
/// what a list of runs should read as.
async function listJobs(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const limit = intParam(params, "limit", 50, 1, 200);
  const notebookId = optionalString(params, "notebook_id");
  const state = optionalString(params, "state");

  if (state !== null && !isJobState(state)) {
    throw badRequest(`"${state}" is not a valid state. Allowed: ${JOB_STATES.join(", ")}.`);
  }

  let query = db
    .from("jobs")
    .select(JOB_COLUMNS)
    .order("planned_start_at", { ascending: false })
    .limit(limit);

  if (notebookId !== null) query = query.eq("notebook_id", notebookId);
  if (state !== null) query = query.eq("state", state);

  const { data, error } = await query;
  if (error) throw upstream(`Could not load the run list: ${error.message}`);

  const rows = (data ?? []) as Array<Record<string, unknown>>;
  return itemsResponse(await withNames(db, rows));
}

async function getJob(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");

  const found = await db.from("jobs").select(JOB_COLUMNS).eq("id", id).maybeSingle();
  if (found.error) throw upstream(`Could not look up the run: ${found.error.message}`);
  if (!found.data) throw notFound("This run no longer exists. Refresh the list.");

  const [row] = await withNames(db, [found.data as Record<string, unknown>]);
  return itemResponse(row);
}

/// A job's timeline, newest first, which is the order the detail screen shows.
///
/// There is no `kind` column on `job_events` -- the level plus the message is
/// the whole record -- so nothing here invents one.
async function listEvents(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const jobId = uuidParam(params, "job_id");
  const limit = intParam(params, "limit", 200, 1, 500);

  const { data, error } = await db
    .from("job_events")
    .select("id,job_id,level,message,payload,at")
    .eq("job_id", jobId)
    .order("at", { ascending: false })
    .limit(limit);

  if (error) throw upstream(`Could not load the run timeline: ${error.message}`);
  return itemsResponse(data ?? []);
}

/// Create a `pending` job for a notebook, due now.
///
/// Three column values carry the whole meaning of this op:
///
///   * `schedule_id` is left **null**, and that is load-bearing. It is what
///     exempts the row from `jobs_one_per_schedule_instant`, the unique partial
///     index on `(schedule_id, planned_start_at)`. A manual run is not the same
///     event as a scheduled one, so pressing the button twice means it twice --
///     which is the schema's stated intent, not an accident.
///   * `kaggle_version_id` is left **null**, exactly as `tick.py` leaves it. The
///     worker resolves the newest stored version itself at start time
///     (`worker.py::start` does `order=version.desc, limit=1`); naming a version
///     here would be a claim about what ran that the runner can contradict.
///   * `state` is `pending`, never anything else. Nothing in this function
///     starts a run.
async function runNow(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const notebookId = uuidParam(params, "notebook_id");

  const notebook = await db
    .from("notebooks")
    .select("id,account_id,kaggle_ref,display_name,is_active")
    .eq("id", notebookId)
    .maybeSingle();

  if (notebook.error) throw upstream(`Could not look up the notebook: ${notebook.error.message}`);
  if (!notebook.data) throw notFound("This notebook no longer exists. Refresh the list.");

  // A deactivated notebook is refused here for the same reason
  // `schedules/index.ts::assertNotebookRunnable` refuses to schedule one: the
  // switch means "do not run this", and starting it anyway would make the
  // switch a lie. The message names the fix rather than only the problem.
  if (notebook.data.is_active === false) {
    throw conflict("This notebook is turned off. Turn it on and try again.");
  }

  // A notebook with no stored version cannot be started: the worker downloads
  // `notebook_versions` for its source and would crash on an empty result,
  // recording only `WORKER_CRASH`. `addNotebook` pulls version 1 and deletes the
  // row again if that pull fails, so this should be unreachable -- but a wasted
  // five-minute wait followed by an unexplained failure is a bad enough outcome
  // to spend one count query on.
  const versions = await db
    .from("notebook_versions")
    .select("id", { count: "exact", head: true })
    .eq("notebook_id", notebookId);

  if (versions.error) throw upstream(`Could not look up the notebook's version: ${versions.error.message}`);
  if ((versions.count ?? 0) === 0) {
    throw conflict("This notebook has no version pulled yet, so it cannot be run.");
  }

  const startAt = new Date();
  const stopAt = new Date(startAt.getTime() + MANUAL_RUN_MINUTES * 60 * 1000);

  const inserted = await db
    .from("jobs")
    .insert({
      schedule_id: null,
      notebook_id: notebookId,
      account_id: notebook.data.account_id,
      kaggle_version_id: null,
      state: "pending",
      planned_start_at: startAt.toISOString(),
      planned_stop_at: stopAt.toISOString(),
      // The window above, plus Kaggle's own margin, so the watchdog deadline is
      // always reached before Kaggle's session limit is.
      timeout_seconds: MANUAL_RUN_MINUTES * 60 + STOP_BUFFER_SECONDS,
      // 'manual' is the third value `0002_cancel_channel.sql` added to the
      // column's CHECK, and the worker copies it into `stop_reason` when the job
      // ends -- so the record says a person asked for this rather than a
      // schedule.
      stop_reason_pref: "manual",
    })
    .select(JOB_COLUMNS)
    .single();

  if (inserted.error || !inserted.data) {
    throw upstream(`Could not create the run: ${inserted.error?.message ?? "unknown error"}`);
  }

  const [row] = await withNames(db, [inserted.data as Record<string, unknown>]);
  return itemResponse(row);
}

/// Ask a run to stop.
///
/// **This deliberately does not call `km_claim_job`.** That RPC has a `cancel`
/// mode, and reading its `WHERE` in 0001 shows why it cannot be used here: it
/// selects `state in ('pending','claimed','running')`, and `watching` is **not**
/// in that list. `watching` means "a worker has stepped back but the run is
/// still going", which is precisely a live run from the user's point of view --
/// the app's own poll treats it as live and offers Cancel for it. So the RPC
/// would silently match nothing for a watching job and the button would appear
/// to do nothing. The same rule is reproduced below with that state included.
///
/// The two branches mirror 0001's semantics exactly:
///
///   * `pending` / `claimed` -- nothing is running yet, so the cancel is
///     finalised immediately: `cancelled`, `stop_reason = cancelled_by_user`,
///     `error_code = CANCELLED_BY_USER`, `finished_at` stamped. Finalising the
///     `pending` case is the one that matters most, because `tick` claims
///     pending rows in `planned_start_at` order and would otherwise start the
///     run and burn a real Kaggle push after the user cancelled it.
///   * anything else -- the flag is set and the row is left alone. The watchdog
///     inside the notebook polls it through `km_cancel_poll` and exits.
///
/// One honest caveat: for a `claimed` job a worker may already be mid-push, and
/// `worker.py` writes `state = 'running'` unconditionally after the push is
/// accepted, briefly overwriting the `cancelled` state set here. The cancel flag
/// is what actually stops the run, so the run still ends cancelled -- but the
/// row can spend a few seconds looking like it went back to running.
async function cancelJob(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");

  const found = await db.from("jobs").select(JOB_COLUMNS).eq("id", id).maybeSingle();
  if (found.error) throw upstream(`Could not look up the run: ${found.error.message}`);
  if (!found.data) throw notFound("This run no longer exists. Refresh the list.");

  const state = String(found.data.state ?? "");

  // The app only offers Cancel for a live or pending job, so this is a race
  // guard rather than a path a user normally reaches. Saying so plainly beats
  // setting a flag on a finished run, which would read as "cancelled" in the
  // history while nothing had been cancelled.
  if (FINAL_STATES.includes(state)) {
    throw conflict("This run has already finished, so there is nothing to cancel.");
  }

  const patch: Record<string, unknown> = state === "pending" || state === "claimed"
    ? {
      state: "cancelled",
      cancel_requested: true,
      stop_reason: "cancelled_by_user",
      error_code: "CANCELLED_BY_USER",
      finished_at: new Date().toISOString(),
    }
    : { cancel_requested: true };

  const updated = await db
    .from("jobs")
    .update(patch)
    .eq("id", id)
    .select(JOB_COLUMNS)
    .maybeSingle();

  if (updated.error) throw upstream(`Could not cancel the run: ${updated.error.message}`);
  if (!updated.data) throw notFound("This run no longer exists. Refresh the list.");

  // Returned as it now stands, which is the contract: the app must be able to
  // show "cancel sent" without claiming the run has already stopped.
  const [row] = await withNames(db, [updated.data as Record<string, unknown>]);
  return itemResponse(row);
}

/// Attach the notebook's and account's names to job rows.
///
/// `Job.fromJson` reads `notebooks.display_name`, `notebooks.kaggle_ref` and
/// `accounts.label` as embeds, and also accepts the flat `notebook_name`,
/// `notebook_ref` and `account_label`. Both are sent, which is what
/// `schedules/index.ts::listSchedules` already does -- the flat names are what a
/// hand-written curl call would reach for, and the embedded objects are the
/// shape a handful of the app's widgets expect.
///
/// Fetched as two extra queries rather than as a PostgREST embed, again matching
/// schedules: a missing notebook then degrades to a null name instead of
/// dropping the whole job from the list, and a job row disappearing is a far
/// worse failure than a job row with no name.
async function withNames(
  db: ReturnType<typeof admin>,
  rows: Array<Record<string, unknown>>,
): Promise<Array<Record<string, unknown>>> {
  if (rows.length === 0) return rows;

  const notebookIds = [...new Set(rows.map((row) => String(row["notebook_id"])))];
  const accountIds = [...new Set(rows.map((row) => String(row["account_id"])))];

  const notebooks = await db
    .from("notebooks")
    .select("id,display_name,kaggle_ref")
    .in("id", notebookIds);

  if (notebooks.error) throw upstream(`Could not load notebooks: ${notebooks.error.message}`);

  const accounts = await db.from("accounts").select("id,label").in("id", accountIds);
  if (accounts.error) throw upstream(`Could not load accounts: ${accounts.error.message}`);

  const notebookById = new Map<string, { display_name: string; kaggle_ref: string }>();
  for (const row of (notebooks.data ?? []) as Array<{
    id: string;
    display_name: string;
    kaggle_ref: string;
  }>) {
    notebookById.set(row.id, { display_name: row.display_name, kaggle_ref: row.kaggle_ref });
  }

  const labelById = new Map<string, string>();
  for (const row of (accounts.data ?? []) as Array<{ id: string; label: string }>) {
    labelById.set(row.id, row.label);
  }

  return rows.map((row) => {
    const notebook = notebookById.get(String(row["notebook_id"]));
    const label = labelById.get(String(row["account_id"]));

    return {
      ...row,
      notebook_name: notebook?.display_name ?? null,
      notebook_ref: notebook?.kaggle_ref ?? null,
      account_label: label ?? null,
      notebooks: notebook
        ? { display_name: notebook.display_name, kaggle_ref: notebook.kaggle_ref }
        : null,
      accounts: label ? { label } : null,
    };
  });
}

function isJobState(value: string): value is (typeof JOB_STATES)[number] {
  return (JOB_STATES as readonly string[]).includes(value);
}

// ------------------------------------------------------------------ entry
//
// Supabase requires an entry point: either this `Deno.serve(...)` call or an
// `export default { fetch }`. Both are the same thing to the platform, and one
// is mandatory -- without it the function deploys and then serves nothing at
// all, which is the failure this file previously had.
//
// It is deliberately this thin. The whole of the work lives in the handler
// above, so every function in this tree reads the same way and a new op is
// added in one place. `parseRequest` reads `op` from the query string or the
// body and merges the rest, and `errorResponse` is the single place an error
// becomes a response -- it turns an `ApiError` into its English sentence and
// anything else into a generic 500, logging both server-side.
Deno.serve(async (request: Request) => {
  try {
    const { op, params } = await parseRequest(request);
    return await handleJobs(op, params);
  } catch (error) {
    return errorResponse(error);
  }
});