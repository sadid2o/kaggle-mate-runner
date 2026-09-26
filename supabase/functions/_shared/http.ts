// Response shapes and the error contract, in one place.
//
// The app reads exactly three shapes, and they are frozen: a list op answers
// `{ "items": [...] }`, a single-row op answers `{ "item": {...} }`, and an
// error answers a non-2xx with `{ "error": "<short sentence>" }`. This
// module is the only place those shapes are produced, so a new op cannot invent
// a fourth one by accident.

/// Every failure the client is allowed to see.
///
/// The message is written for the person using the app and it
/// names the actual problem. That matters more than it looks: the app surfaces
/// `details['error']` verbatim and falls back to a generic sentence only when
/// this field is absent, so a vague message here becomes a vague message the
/// user cannot act on.
export class ApiError extends Error {
  readonly status: number;
  readonly userMessage: string;

  constructor(status: number, userMessage: string) {
    super(userMessage);
    this.name = "ApiError";
    this.status = status;
    this.userMessage = userMessage;
  }
}

/// 400 — the request cannot be acted on as sent.
export const badRequest = (message: string) => new ApiError(400, message);

/// 404 — the thing named does not exist.
export const notFound = (message: string) => new ApiError(404, message);

/// 409 — the request is valid but the current state refuses it.
export const conflict = (message: string) => new ApiError(409, message);

/// 502 — something upstream (Kaggle, Storage) failed.
export const upstream = (message: string) => new ApiError(502, message);

export function itemResponse(item: unknown, status = 200): Response {
  return Response.json({ item }, { status });
}

export function itemsResponse(items: unknown[], status = 200): Response {
  return Response.json({ items }, { status });
}

/// A bare acknowledgement, for ops whose caller ignores the body.
///
/// The contract has no shape for "done", so this sends `{ "item": {...} }` too
/// rather than an empty 204: the app's `_one` throws on an empty body, and an op
/// that returns nothing would look like a server fault.
export function okResponse(): Response {
  return Response.json({ item: { ok: true } });
}

export function errorResponse(error: unknown): Response {
  if (error instanceof ApiError) {
    // Logged server-side, never returned: the log line is for the operator, the
    // message is for the user, and mixing them would leak internals or confuse
    // the person reading it.
    console.error(`[km] ${error.status}: ${error.userMessage}`);
    return Response.json({ error: error.userMessage }, { status: error.status });
  }

  const detail = error instanceof Error ? error.message : String(error);
  console.error(`[km] unhandled: ${detail}`);
  return Response.json(
    { error: "An unexpected server error occurred. Please try again." },
    { status: 500 },
  );
}
