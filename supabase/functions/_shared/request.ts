// Parsing the request, and routing on `op`.
//
// Two calling conventions reach these functions and both must work:
//
//   * The app sends reads as **query parameters** (`?op=list&limit=50`) and
//     writes as a **JSON body** (`{"op":"create", ...}`).
//   * A human with curl will do whichever is easier, which is usually a body for
//     everything.
//
// So `op` is looked for in the query first and then the body, while individual
// parameters are merged with the body taking precedence (an explicit body value
// is more specific than a stray query string). Values are read as strings and
// converted at the point of use, because a query parameter is always a string
// and a body value may be a string or a number.

import { badRequest } from "./http.ts";

export type Params = Record<string, unknown>;

export interface KmRequest {
  op: string;
  params: Params;
}

/// Parse the body as JSON, tolerating absent or non-JSON bodies.
///
/// Deliberately forgiving: a GET-style read has no body at all, and an empty
/// body must not be an error for an op that needs nothing but `?op=health`.
export async function readBody(request: Request): Promise<Params> {
  const text = await request.text();
  if (!text.trim()) return {};
  try {
    const parsed = JSON.parse(text);
    // A JSON body that is not an object (a bare array or string) is a client
    // mistake worth naming rather than silently discarding.
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw badRequest("The request body has the wrong type.");
    }
    return parsed as Params;
  } catch (error) {
    if (error instanceof Error && error.name === "ApiError") throw error;
    throw badRequest("The request body cannot be read (it is not JSON).");
  }
}

export async function parseRequest(request: Request): Promise<KmRequest> {
  const url = new URL(request.url);
  const body = await readBody(request);

  const params: Params = {};
  for (const [key, value] of url.searchParams.entries()) params[key] = value;
  // Body wins over query string, for the reason in the header comment.
  for (const [key, value] of Object.entries(body)) params[key] = value;

  const op = params["op"];
  if (typeof op !== "string" || op.trim() === "") {
    throw badRequest("No operation was named (missing \"op\").");
  }

  return { op: op.trim(), params };
}

// ---- value readers -------------------------------------------------------
// Each throws the specific message that fits the field, so a missing
// value never reaches Postgres and comes back as an unreadable constraint error.

export function stringParam(params: Params, name: string): string {
  const raw = params[name];
  if (typeof raw !== "string" || raw.trim() === "") {
    throw badRequest(`"${name}" was not provided.`);
  }
  return raw.trim();
}

export function optionalString(params: Params, name: string): string | null {
  const raw = params[name];
  if (raw === undefined || raw === null) return null;
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  return trimmed === "" ? null : trimmed;
}

export function boolParam(params: Params, name: string): boolean {
  const raw = params[name];
  if (typeof raw === "boolean") return raw;
  if (raw === "true") return true;
  if (raw === "false") return false;
  throw badRequest(`"${name}" must be true or false.`);
}

export function optionalBool(params: Params, name: string): boolean | null {
  const raw = params[name];
  if (raw === undefined || raw === null) return null;
  if (typeof raw === "boolean") return raw;
  if (raw === "true") return true;
  if (raw === "false") return false;
  return null;
}

export function intParam(params: Params, name: string, fallback: number, min: number, max: number): number {
  const raw = params[name];
  if (raw === undefined || raw === null || raw === "") return fallback;
  // A number may arrive as a JSON number or a query string; `Number` handles
  // both, and anything that is not finite is rejected rather than coerced to
  // NaN and sent to Postgres.
  const value = typeof raw === "number" ? raw : Number(String(raw));
  if (!Number.isFinite(value)) {
    throw badRequest(`"${name}" must be a number.`);
  }
  return Math.min(Math.max(Math.trunc(value), min), max);
}

/// A UUID, checked here so the error names the field instead of surfacing
/// Postgres's "invalid input syntax for type uuid".
export function uuidParam(params: Params, name: string): string {
  const value = stringParam(params, name);
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  if (!uuid.test(value)) {
    throw badRequest(`"${name}" is not a valid id.`);
  }
  return value;
}

/// A short text value with a length bound, for things like labels.
export function textParam(params: Params, name: string, maxLength: number): string {
  const value = stringParam(params, name);
  if (value.length > maxLength) {
    throw badRequest(`"${name}" is too long (at most ${maxLength} characters).`);
  }
  return value;
}

/// A `time` column value. The app already sends "HH:MM:SS" (see
/// `Schedule.toRow`), but a curl caller may send "HH:MM", so both are accepted
/// and normalised to the form Postgres stores.
export function timeParam(params: Params, name: string): string {
  const raw = stringParam(params, name);
  const match = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(raw);
  if (!match) {
    throw badRequest(`"${name}" must be a clock time in "HH:MM" form.`);
  }
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  const second = match[3] ? Number(match[3]) : 0;
  if (hour > 23 || minute > 59 || second > 59) {
    throw badRequest(`"${name}" is not a valid time.`);
  }
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}:${String(second).padStart(2, "0")}`;
}

export function optionalTime(params: Params, name: string): string | null {
  const raw = params[name];
  if (raw === undefined || raw === null || raw === "") return null;
  return timeParam(params, name);
}

/// `days_of_week` as the `smallint[]` the column holds: 0 = Monday .. 6 = Sunday.
///
/// Accepts an array (the app's form) or a Postgres literal like "{0,2,4}" (a
/// curl caller copying from the database), because both are reasonable and only
/// one of them is what the app happens to send.
export function daysParam(params: Params, name: string): number[] {
  const raw = params[name];
  let values: unknown[];

  if (Array.isArray(raw)) {
    values = raw;
  } else if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      values = Array.isArray(parsed) ? parsed : String(raw).replace(/[{}]/g, "").split(",");
    } catch {
      values = raw.replace(/[{}]/g, "").split(",");
    }
  } else {
    throw badRequest("The days of the week were not provided.");
  }

  const days = values
    .map((value) => (typeof value === "number" ? value : Number(String(value).trim())))
    .filter((value) => Number.isInteger(value) && value >= 0 && value <= 6);

  if (days.length === 0) {
    throw badRequest("Pick at least one day of the week.");
  }
  // Deduplicated and sorted so the stored array is canonical; the column CHECK
  // would accept duplicates but they are meaningless.
  return [...new Set(days)].sort((a, b) => a - b);
}

/// A `date` column value. The app sends a bare `YYYY-MM-DD`.
export function optionalDate(params: Params, name: string): string | null {
  const raw = optionalString(params, name);
  if (raw === null) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
  if (!match) {
    throw badRequest(`"${name}" must be a date in "YYYY-MM-DD" form.`);
  }
  return `${match[1]}-${match[2]}-${match[3]}`;
}
