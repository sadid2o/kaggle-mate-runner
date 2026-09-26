// `alerts` — the in-app notification list.
//
// Read and mark-as-read, and nothing else. That is the whole surface the app
// calls, and it is deliberate: an alert is a record of something the runner
// decided, so the app has no business creating one.
//
// READ THIS BEFORE WONDERING WHY THE LIST IS EMPTY.
//
// Nothing currently writes this table. Grepped `kaggle-mate-runner/runner/*.py`
// on 2026-09-26: `tick.py` and `worker.py` write `jobs` and `job_events` only,
// and no file inserts a row into `alerts`. So this function is correct and the
// table is empty, and those two facts are not in conflict. A writer still has to
// be added -- most naturally in `worker.py`, where a job reaches a final state
// and the failure is already known -- and deciding *which* outcomes deserve an
// alert row is a product decision, which is why one was not guessed at here.
// See `_PROGRESS.md` for the same note.
//
// Authorization: no credential arrives from the app (see `_shared/db.ts`), so
// there is none to check. Every query runs with the service key, which never
// leaves the server.

import { admin } from "../_shared/db.ts";
import {
  badRequest,
  errorResponse,
  itemsResponse,
  okResponse,
  upstream,
} from "../_shared/http.ts";
import { intParam, type Params, parseRequest } from "../_shared/request.ts";

/// Every column of the table, listed rather than `*`. `AppAlert.fromJson` reads
/// all of them: `read_at` is what makes the unread badge work, and `severity`
/// is what picks the row's colour.
const ALERT_COLUMNS = "id,job_id,severity,title,body,read_at,created_at";

export async function handleAlerts(op: string, params: Params): Promise<Response> {
  const db = admin();

  switch (op) {
    case "list":
      return listAlerts(db, params);
    case "mark_read":
      return markRead(db, params);
    case "mark_all_read":
      return markAllRead(db, params);
    default:
      throw badRequest(
        `"${op}" is not an operation. alerts supports: list, mark_read, mark_all_read.`,
      );
  }
}

/// Newest first, and **unread rows are not filtered out**.
///
/// That is on purpose twice over. The list screen shows read and unread alerts
/// together -- an alert that vanished the moment it was marked read would look
/// like it had been deleted -- and `AppAlert.isUnread` is computed client-side
/// from `read_at`, so filtering here would remove the field's whole purpose.
/// The index `alerts_unread_idx` covers the `read_at is null` case for any
/// reader that does want only unread rows, which is not this op.
async function listAlerts(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const limit = intParam(params, "limit", 100, 1, 500);

  const { data, error } = await db
    .from("alerts")
    .select(ALERT_COLUMNS)
    .order("created_at", { ascending: false })
    .limit(limit);

  if (error) throw upstream(`Could not fetch the alert list: ${error.message}`);
  return itemsResponse(data ?? []);
}

/// Mark one alert read.
///
/// The id is an **integer**, not a UUID. `alerts.id` is `bigserial` in 0001, and
/// the app passes an `int` (`AppAlert.id` is `int`, and `markAlertRead(int id)`
/// sends it in the body). So `uuidParam` would be wrong here and would reject
/// every real call -- the reader is `intParam` instead.
///
/// Setting `read_at` is idempotent by construction: the update is a no-op on a
/// row that is already read, and an already-read alert staying read is the only
/// sensible outcome.
async function markRead(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  // Checked for presence before `intParam`, because `intParam` needs a fallback
  // and there is no sensible one for a required id: its default of "absent means
  // 50" is right for a limit and would silently become "update row 0, or row 1
  // after clamping" here. Naming the missing field is the only honest answer.
  const raw = params["id"];
  if (raw === undefined || raw === null || raw === "") {
    throw badRequest('"id" was not provided.');
  }

  const id = intParam(params, "id", 0, 1, Number.MAX_SAFE_INTEGER);

  const updated = await db
    .from("alerts")
    .update({ read_at: new Date().toISOString() })
    .eq("id", id)
    .select(ALERT_COLUMNS)
    .maybeSingle();

  if (updated.error) throw upstream(`Could not save the alert as read: ${updated.error.message}`);
  // Not a 404. The app's `markAlertRead` ignores the body entirely, and a
  // conflict here would surface as a toast about an alert the user does not care
  // about -- the alert was read (or was already gone), which is what they asked
  // for either way.
  if (!updated.data) console.error(`[km] mark_read: alert ${id} not found`);

  return okResponse();
}

/// Mark every unread alert read, in one statement.
///
/// Scoped to `read_at is null` rather than updating the whole table. The rows
/// are equivalent either way, but this way the operation still means what its
/// name says if `read_at` ever grows a second meaning, and it touches strictly
/// fewer rows -- which is the difference between a rewrite of the table and an
/// index-only update on `alerts_unread_idx`.
async function markAllRead(db: ReturnType<typeof admin>, _params: Params): Promise<Response> {
  const updated = await db
    .from("alerts")
    .update({ read_at: new Date().toISOString() })
    .is("read_at", null);

  if (updated.error) throw upstream(`Could not save the alerts as read: ${updated.error.message}`);
  return okResponse();
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
    return await handleAlerts(op, params);
  } catch (error) {
    return errorResponse(error);
  }
});