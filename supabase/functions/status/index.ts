// `status` — "is the whole thing actually alive?"
//
// This is the question a user has after setting a schedule and seeing nothing
// happen, and it is answered here by one call rather than four, so the app's
// refresh is one round trip and the rule for "the runner looks stalled" lives
// next to the data it reads.
//
// THE ONE SHAPE EXCEPTION IN THIS TREE.
//
// Every other op answers `{ "item": ... }` or `{ "items": [...] }` through
// `_shared/http.ts`. This one answers a **flat** snake_case object, and that is
// not a slip: the app reads it at the top level. `AppRepository.runnerStatus`
// does `data['is_configured']`, `data['pending_jobs']` and so on straight off
// the response -- there is no `item` or `items` key anywhere in that method. So
// wrapping this body would make every field read as null and `RunnerStatus`
// would render as a permanently unconfigured system. The body is built with
// `Response.json` directly for that reason, and it is the only file that does
// so.
//
// Everything here is derived from tables the runner already writes -- `accounts`
// and `jobs`. No new endpoint, no new table, no heartbeat row.
//
// Authorization: no credential arrives from the app (see `_shared/db.ts`), so
// there is none to check. Every query runs with the service key, which never
// leaves the server.

import { admin } from "../_shared/db.ts";
import { badRequest, errorResponse, upstream } from "../_shared/http.ts";
import { type Params, parseRequest } from "../_shared/request.ts";

/// The states a job is in once a worker has hold of it and until it is done.
/// `watching` counts: a worker has stepped back, but the run is still going on
/// Kaggle, which is exactly what "live" means to a user watching the screen.
/// Kept in step with `jobs_live_idx` in 0001, which is the index Postgres uses
/// for this exact set.
const LIVE_STATES = ["claimed", "running", "watching"];

/// How far back "recently" reaches for the failure count. A day is long enough
/// to notice a broken token on the next app open and short enough that an old
/// failure does not keep a red banner up forever.
const FAILED_RECENT_HOURS = 24;

export async function handleStatus(op: string, params: Params): Promise<Response> {
  const db = admin();

  switch (op) {
    case "health":
      return health(db, params);

    default:
      throw badRequest(`"${op}" is not an operation. status supports: health.`);
  }
}

async function health(db: ReturnType<typeof admin>, _params: Params): Promise<Response> {
  // The four counts and the two instants are independent, so they are issued
  // together rather than in sequence. These are all index-only or single-row
  // reads against small tables, so the parallelism is worth it on a screen the
  // app refreshes on a timer.
  const [activeAccounts, pending, live, failedRecently, newestJob, nextPending] = await Promise.all([
    // `head: true` asks Postgres for the count and no rows: the rows themselves
    // have no use here and would be the whole account table over the wire.
    db.from("accounts").select("id", { count: "exact", head: true }).eq("is_active", true),

    db.from("jobs").select("id", { count: "exact", head: true }).eq("state", "pending"),

    db.from("jobs").select("id", { count: "exact", head: true }).in("state", LIVE_STATES),

    // `finished_at`, not `created_at`. A job that failed is finished, and
    // `worker.py::_finish` stamps `finished_at` for every state in its
    // `FINAL_STATES` set -- checked, not assumed -- so every failed job has one.
    // Using `finished_at` means a long run that was created before the window
    // but failed inside it is still counted, which is the truthful reading of
    // "failed recently".
    db
      .from("jobs")
      .select("id", { count: "exact", head: true })
      .eq("state", "failed")
      .gte("finished_at", new Date(Date.now() - FAILED_RECENT_HOURS * 3600 * 1000).toISOString()),

    // One row, newest first. `maybeSingle` is what makes "no jobs have ever been
    // created" a null rather than an error, which is the state a fresh install
    // is in and must not read as a server fault.
    db.from("jobs").select("created_at").order("created_at", { ascending: false }).limit(1).maybeSingle(),

    db
      .from("jobs")
      .select("planned_start_at")
      .eq("state", "pending")
      .order("planned_start_at", { ascending: true })
      .limit(1)
      .maybeSingle(),
  ]);

  for (const result of [activeAccounts, pending, live, failedRecently, newestJob, nextPending]) {
    // Ordered by which query failed would be nicer, but naming the table that
    // broke is the part that helps an operator. All of them read `accounts` or
    // `jobs`, so the sentence below is accurate without listing six variants.
    if (result.error) throw upstream(`Could not determine the runner status: ${result.error.message}`);
  }

  const lastJobAt = (newestJob.data?.created_at as string | null) ?? null;

  // `last_tick_at` is the same instant as `last_job_at`, and that is not a
  // shortcut -- it is what the field means. `lib/data/models/alert.dart`'s
  // `lastTickAt` is documented as "The newest `jobs.created_at`. The tick
  // workflow writes a job row every time it decides something is due, so this
  // doubles as a liveness signal."
  //
  // What it cannot be, and why: there is no real heartbeat to read. `tick.py`
  // writes no heartbeat row anywhere -- it only *reads* `settings`, and the only
  // tables it writes are `jobs` and `job_events` (grep, 2026-09-26). So a tick
  // that ran and found nothing due leaves no trace at all, and this value can
  // never be sharper than "the last time any job row was created". Adding a real
  // heartbeat would need a writer in `tick.py`; it is not invented here.
  const lastTickAt = lastJobAt;

  return Response.json({
    // False until the user has added at least one account. Everything else below
    // is meaningless without a token, so this is the one field the home screen
    // reads first.
    is_configured: (activeAccounts.count ?? 0) > 0,
    last_tick_at: lastTickAt,
    last_job_at: lastJobAt,
    // The soonest job still waiting to start. Null when nothing is pending,
    // which the app reads as "nothing scheduled soon" rather than an error.
    next_job_at: (nextPending.data?.planned_start_at as string | null) ?? null,
    pending_jobs: pending.count ?? 0,
    live_jobs: live.count ?? 0,
    failed_recently: failedRecently.count ?? 0,
  });
}

// ------------------------------------------------------------------ entry
//
// Supabase requires an entry point: either this `Deno.serve(...)` call or an
// `export default { fetch }`. Both mean the same thing to the platform, and one
// is mandatory -- without it the function deploys and then serves nothing. See
// the matching block in `jobs/index.ts`; every function in this tree has one and
// they are deliberately identical in shape.
Deno.serve(async (request: Request) => {
  try {
    const { op, params } = await parseRequest(request);
    return await handleStatus(op, params);
  } catch (error) {
    return errorResponse(error);
  }
});