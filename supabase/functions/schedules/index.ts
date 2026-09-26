// `schedules` — when a notebook should run.
//
// The times are wall-clock Asia/Dhaka and are stored as bare `time` values, not
// instants: the whole point is that "08:00" stays 08:00 when the country's UTC
// offset changes, and the engine, not the database, decides which UTC moment
// that is. `runner/schedule_engine.py::DueJob.timeout_seconds = span + 300`
// reads `duration_minutes` and `stop_at_time` back out, so the two fields below
// are the ones that decide how long a run is allowed to live.
//
// The app sends these already shaped for the column (see `Schedule.toRow`):
// `start_time` and `stop_at_time` as "HH:MM:SS", `start_date`/`end_date` as bare
// "YYYY-MM-DD", and `days_of_week` as a list of 0-6 with 0 = Monday. The
// readers in `_shared/request.ts` accept a sloppier shape too, so a curl call
// with "08:00" or "{0,2,4}" works -- but nothing here invents a format the
// engine cannot read.
//
// One engine rule is enforced here rather than left to fail later:
// `_resolve_stop` takes the earlier of "duration elapsed" and "stop_at_time
// reached", so both are needed exactly when they can still produce a stop
// before the other. Passing neither duration nor stop time would leave the
// engine with no stop condition at all, which is a job that never ends.

import { admin } from "../_shared/db.ts";
import {
  badRequest,
  conflict,
  errorResponse,
  itemResponse,
  itemsResponse,
  notFound,
  okResponse,
  upstream,
} from "../_shared/http.ts";
import {
  daysParam,
  intParam,
  optionalBool,
  optionalDate,
  optionalString,
  optionalTime,
  type Params,
  parseRequest,
  timeParam,
  uuidParam,
} from "../_shared/request.ts";

/// The columns the app's `Schedule.fromJson` reads, and nothing else. The two
/// notebook fields it also reads are joined below, because the app's schedule
/// list shows the notebook name and has no second call to fetch it.
const SCHEDULE_COLUMNS =
  "id,notebook_id,days_of_week,start_time,duration_minutes,stop_at_time,timezone,start_date,end_date,is_active,created_at,overrides";

/// A stop preference, checked here because 0002 added
/// `jobs_stop_reason_pref_check` and the column that holds it is only reachable
/// through this function.
const STOP_REASON_PREFS = new Set(["duration", "clock_time", "manual"]);

export async function handleSchedules(op: string, params: Params): Promise<Response> {
  const db = admin();

  switch (op) {
    case "list":
      return listSchedules(db, params);
    case "create":
      return createSchedule(db, params);
    case "update":
      return updateSchedule(db, params);
    case "delete":
      return deleteSchedule(db, params);
    default:
      throw badRequest(`"${op}" is not a valid operation. schedules supports: list, create, update, delete.`);
  }
}

async function listSchedules(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const notebookId = optionalString(params, "notebook_id");

  let query = db.from("schedules").select(SCHEDULE_COLUMNS).order("created_at", { ascending: false });
  if (notebookId !== null) query = query.eq("notebook_id", notebookId);

  const { data, error } = await query;
  if (error) throw upstream(`Could not load the schedule list: ${error.message}`);

  const rows = (data ?? []) as Array<Record<string, unknown>>;
  if (rows.length === 0) return itemsResponse([]);

  // Joined in one extra query rather than a PostgREST embed, so a missing
  // notebook degrades to an empty name instead of dropping the whole schedule
  // from the list -- the app tolerates a null `notebook_name` and does not
  // tolerate a schedule silently vanishing.
  const ids = [...new Set(rows.map((row) => String(row["notebook_id"])))];
  const notebooks = await db.from("notebooks").select("id,display_name,kaggle_ref").in("id", ids);
  if (notebooks.error) throw upstream(`Could not load notebooks: ${notebooks.error.message}`);

  const byId = new Map<string, { display_name: string; kaggle_ref: string }>();
  for (const row of (notebooks.data ?? []) as Array<{
    id: string;
    display_name: string;
    kaggle_ref: string;
  }>) {
    byId.set(row.id, { display_name: row.display_name, kaggle_ref: row.kaggle_ref });
  }

  return itemsResponse(
    rows.map((row) => {
      const notebook = byId.get(String(row["notebook_id"]));
      return {
        ...row,
        // Both the flat names and an embedded object are sent: `Schedule.fromJson`
        // accepts either (`json['notebook_name']` or `joinedNotebook['display_name']`),
        // and anything reading this API by hand gets the obvious form.
        notebook_name: notebook?.display_name ?? null,
        notebook_ref: notebook?.kaggle_ref ?? null,
        notebooks: notebook
          ? { display_name: notebook.display_name, kaggle_ref: notebook.kaggle_ref }
          : null,
      };
    }),
  );
}

async function createSchedule(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const notebookId = uuidParam(params, "notebook_id");
  const row = await buildRow(db, params, notebookId, false);

  const inserted = await db
    .from("schedules")
    .insert({ notebook_id: notebookId, ...row })
    .select(SCHEDULE_COLUMNS)
    .single();

  if (inserted.error || !inserted.data) {
    throw upstream(`Could not save the schedule: ${inserted.error?.message ?? "unknown error"}`);
  }

  return itemResponse(await withNotebook(db, inserted.data as Record<string, unknown>));
}

async function updateSchedule(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");

  const existing = await db
    .from("schedules")
    .select("id,notebook_id")
    .eq("id", id)
    .maybeSingle();

  if (existing.error) throw upstream(`Could not look up the schedule: ${existing.error.message}`);
  if (!existing.data) throw notFound("This schedule no longer exists. Refresh the list.");

  // `setScheduleActive` on the app side sends nothing but `is_active`. Making a
  // full row mandatory would turn that one-toggle call into a read-modify-write
  // the app has no reason to do, so only the fields actually present are
  // applied and the rest of the row is left alone.
  //
  // The `true` is the fourth argument. A fifth `null` used to sit before it,
  // left over from an older signature: every parameter after the shift landed
  // one slot early, so `partial` received `null` -- which is falsy, so partial
  // mode silently turned itself off and the toggle demanded all eight schedule
  // fields. `deno check` rejected the arity; the behaviour it was hiding is why
  // this is worth a comment rather than a quiet deletion.
  const row = await buildRow(db, params, existing.data.notebook_id, true);
  if (Object.keys(row).length === 0) {
    throw badRequest("Nothing was provided to change.");
  }

  const updated = await db
    .from("schedules")
    .update(row)
    .eq("id", id)
    .select(SCHEDULE_COLUMNS)
    .single();

  if (updated.error || !updated.data) {
    throw upstream(`Could not update the schedule: ${updated.error?.message ?? "unknown error"}`);
  }

  return itemResponse(await withNotebook(db, updated.data as Record<string, unknown>));
}

async function deleteSchedule(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");

  // Jobs are deliberately not deleted with the schedule. The FK is
  // `on delete set null`, so history survives with `schedule_id` null -- which
  // is the same shape a manual run has, and the schema comment says that is
  // intended. Deleting the run history of a schedule the user removed would
  // erase the only record of what those runs did.
  const deleted = await db.from("schedules").delete().eq("id", id).select("id").maybeSingle();
  if (deleted.error) throw upstream(`Could not delete the schedule: ${deleted.error.message}`);
  if (!deleted.data) throw notFound("This schedule no longer exists. Refresh the list.");

  return okResponse();
}

/// Build the column patch from the request, validating only what was sent.
///
/// `partial` is what separates create from update: on create every field is
/// required, on update a field the caller did not mention must not be reset.
async function buildRow(
  db: ReturnType<typeof admin>,
  params: Params,
  notebookId: string,
  partial = false,
): Promise<Record<string, unknown>> {
  const row: Record<string, unknown> = {};

  const rawDays = params["days_of_week"];
  if (!partial || rawDays !== undefined) {
    row["days_of_week"] = daysParam(params, "days_of_week");
  }

  const rawStart = params["start_time"];
  if (!partial || rawStart !== undefined) {
    row["start_time"] = timeParam(params, "start_time");
  }

  const duration = params["duration_minutes"];
  if (!partial || duration !== undefined) {
    // Bounded well below the worker's own ceiling: `worker.py` stops watching
    // after `MAX_WATCH_SECONDS` (5h30m), so a schedule longer than that could
    // never be watched to completion and would always end in a reap.
    row["duration_minutes"] = intParam(params, "duration_minutes", 60, 1, 330);
  }

  const stopAt = optionalTime(params, "stop_at_time");
  if (!partial || params["stop_at_time"] !== undefined) {
    row["stop_at_time"] = stopAt;
  }

  const timezone = optionalString(params, "timezone");
  if (timezone !== null) {
    // Only the one zone the engine is written for is accepted. `ScheduleSpec`
    // uses `ZoneInfo`, so a zone the runner cannot load would not fail here --
    // it would fail at the next tick, with no clue which schedule did it.
    if (timezone !== "Asia/Dhaka") {
      throw badRequest(`Only "${DHAKA_ONLY}" is supported as a timezone right now.`);
    }
    row["timezone"] = timezone;
  } else if (!partial) {
    row["timezone"] = "Asia/Dhaka";
  }

  for (const field of ["start_date", "end_date"] as const) {
    if (!partial || params[field] !== undefined) {
      row[field] = optionalDate(params, field);
    }
  }

  const overrides = params["overrides"];
  if (overrides !== undefined && overrides !== null) {
    if (typeof overrides !== "object" || Array.isArray(overrides)) {
      throw badRequest('"overrides" must be a JSON object.');
    }
    row["overrides"] = overrides;
  } else if (!partial) {
    row["overrides"] = {};
  }

  const stopReasonPref = optionalString(params, "stop_reason_pref");
  if (stopReasonPref !== null) {
    if (!STOP_REASON_PREFS.has(stopReasonPref)) {
      throw badRequest('"stop_reason_pref" must be duration, clock_time or manual.');
    }
    row["stop_reason_pref"] = stopReasonPref;
  }

  const isActive = optionalBool(params, "is_active");
  if (isActive !== null) row["is_active"] = isActive;

  if (!partial) {
    // `duration_minutes` defaults, so "did the caller give a stop condition at
    // all?" is a question about the request, not about the row. The column
    // itself is `not null`, which is why the default exists -- but a schedule
    // built from nothing but a start time would run on an invented 60-minute
    // duration the user never chose, and stop early without explaining why.
    const gaveDuration = row["duration_minutes"] !== undefined && params["duration_minutes"] !== undefined;
    const gaveStopAt = row["stop_at_time"] !== null;
    if (!gaveDuration && !gaveStopAt) {
      throw badRequest("A run length is required -- give duration_minutes or stop_at_time.");
    }

    await assertNotebookRunnable(db, notebookId);
  }

  return row;
}

const DHAKA_ONLY = "Asia/Dhaka";

/// A schedule on a deactivated notebook is a schedule whose jobs will never
/// start: the engine loads schedules joined to `notebooks` and the tick selects
/// only `is_active=true`, so the row would sit there looking scheduled forever.
/// Refusing it here is a clearer answer than a silent no-op every tick.
async function assertNotebookRunnable(db: ReturnType<typeof admin>, notebookId: string): Promise<void> {
  const notebook = await db
    .from("notebooks")
    .select("id,is_active")
    .eq("id", notebookId)
    .maybeSingle();

  if (notebook.error) throw upstream(`Could not look up the notebook: ${notebook.error.message}`);
  if (!notebook.data) throw notFound("This notebook no longer exists. Refresh the list.");
  if (notebook.data.is_active === false) {
    throw conflict("This notebook is turned off. Turn it on and then create the schedule.");
  }
}

/// Attach the notebook's name and ref to a single schedule row, in the same
/// shape `listSchedules` produces. Without this, creating a schedule would
/// return a row that reads differently from the one in the list, and the UI
/// would show a nameless schedule until the next refresh.
async function withNotebook(
  db: ReturnType<typeof admin>,
  row: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const notebook = await db
    .from("notebooks")
    .select("display_name,kaggle_ref")
    .eq("id", row["notebook_id"])
    .maybeSingle();

  if (notebook.error || !notebook.data) {
    return { ...row, notebook_name: null, notebook_ref: null };
  }

  return {
    ...row,
    notebook_name: notebook.data.display_name,
    notebook_ref: notebook.data.kaggle_ref,
    notebooks: {
      display_name: notebook.data.display_name,
      kaggle_ref: notebook.data.kaggle_ref,
    },
  };
}

// ------------------------------------------------------------------ entry
//
// Supabase requires an entry point: either this `Deno.serve(...)` call or an
// `export default { fetch }`. Both mean the same thing to the platform, and one
// is mandatory -- without it the function deploys and then serves nothing at
// all. See the matching block in `jobs/index.ts`; every function in this tree
// has one and they are deliberately identical in shape.
Deno.serve(async (request: Request) => {
  try {
    const { op, params } = await parseRequest(request);
    return await handleSchedules(op, params);
  } catch (error) {
    return errorResponse(error);
  }
});
