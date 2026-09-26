// Tests for the two `_shared` modules that carry the whole contract of the
// Edge Functions: `request.ts` (what a request may contain) and `http.ts` (what
// a response may look like).
//
// Why these two first. They are pure -- no network, no Supabase, no Deno.env --
// so they need no mocking and can run anywhere, including on a public repo's
// pull requests. And they are where every one of the six functions' inputs and
// outputs pass through, so a regression here is a regression everywhere.
//
// These tests exist because `deno check` does not cover this ground. That was
// measured, not assumed: with a nonsense column name planted in `jobs/index.ts`,
// `deno check` still exited 0, because there are no generated database types.
// A type checker cannot see a wrong string; these tests can.
//
// Behaviour asserted here was read out of the source, not guessed. Where a
// behaviour looks surprising it is called out in a comment, because a surprising
// behaviour that is deliberate is exactly what a test should pin down.

import {
  assert,
  assertEquals,
  assertFalse,
  assertInstanceOf,
  assertStrictEquals,
  assertThrows,
} from "jsr:@std/assert@^1";

import { ApiError, badRequest, errorResponse, itemResponse, itemsResponse, notFound, okResponse, upstream, conflict } from "../_shared/http.ts";
import {
  boolParam,
  daysParam,
  intParam,
  optionalBool,
  optionalDate,
  optionalString,
  optionalTime,
  parseRequest,
  stringParam,
  textParam,
  timeParam,
  uuidParam,
  type Params,
} from "../_shared/request.ts";

const VALID_UUID = "7d444840-9dc0-11d1-b245-5ffdce74fad2";

/// Assert that `fn` throws an `ApiError` with the given status and message.
///
/// Checking the status as well as the message matters: the status is what the
/// app keys off, and a message that says "not found" while carrying a 500 would
/// send the user to the wrong screen.
function assertApiError(fn: () => unknown, status: number, messageIncludes: string): void {
  const error = assertThrows(fn, ApiError);
  assertInstanceOf(error, ApiError);
  assertStrictEquals(error.status, status);
  assert(
    error.userMessage.includes(messageIncludes),
    `expected the message to mention ${JSON.stringify(messageIncludes)}, got ${JSON.stringify(error.userMessage)}`,
  );
}

// ---------------------------------------------------------------- http.ts

Deno.test("http: a single row answers as { item }", async () => {
  const response = itemResponse({ id: 1 });
  assertStrictEquals(response.status, 200);
  assertEquals(await response.json(), { item: { id: 1 } });
});

Deno.test("http: a list answers as { items }", async () => {
  const response = itemsResponse([{ id: 1 }, { id: 2 }]);
  assertStrictEquals(response.status, 200);
  assertEquals(await response.json(), { items: [{ id: 1 }, { id: 2 }] });
});

Deno.test("http: an empty list is still a 200 with an empty array, not a 404", async () => {
  // The app's `_rows` reads `items` and tolerates it being empty; a 404 here
  // would turn "you have no runs yet" into an error screen.
  const response = itemsResponse([]);
  assertStrictEquals(response.status, 200);
  assertEquals(await response.json(), { items: [] });
});

Deno.test("http: okResponse still carries an item, because the app's _one throws on an empty body", async () => {
  const response = okResponse();
  assertStrictEquals(response.status, 200);
  assertEquals(await response.json(), { item: { ok: true } });
});

Deno.test("http: an ApiError becomes its own status and message", async () => {
  const response = errorResponse(badRequest("Pick at least one day of the week."));
  assertStrictEquals(response.status, 400);
  assertEquals(await response.json(), { error: "Pick at least one day of the week." });
});

Deno.test("http: each error helper carries the status it documents", () => {
  assertStrictEquals(badRequest("x").status, 400);
  assertStrictEquals(notFound("x").status, 404);
  assertStrictEquals(conflict("x").status, 409);
  assertStrictEquals(upstream("x").status, 502);
});

Deno.test("http: an unknown error is a 500 that leaks nothing", async () => {
  // The detail must go to the log, never to the client: a raw exception message
  // can name a column, a host or a token.
  const response = errorResponse(new Error("relation \"accounts\" does not exist"));
  assertStrictEquals(response.status, 500);
  const body = await response.json();
  assert(
    !JSON.stringify(body).includes("accounts"),
    "the raw error text must not reach the client",
  );
});

// ------------------------------------------------------- request: strings

Deno.test("stringParam: trims and returns a real string", () => {
  assertStrictEquals(stringParam({ a: "  hi  " }, "a"), "hi");
});

Deno.test("stringParam: rejects a missing, empty or non-string value by name", () => {
  assertApiError(() => stringParam({}, "label"), 400, '"label" was not provided.');
  assertApiError(() => stringParam({ label: "   " }, "label"), 400, '"label" was not provided.');
  assertApiError(() => stringParam({ label: 42 }, "label"), 400, '"label" was not provided.');
});

Deno.test("optionalString: absent and blank both mean null, and a non-string is null rather than an error", () => {
  assertStrictEquals(optionalString({}, "x"), null);
  assertStrictEquals(optionalString({ x: null }, "x"), null);
  assertStrictEquals(optionalString({ x: "  " }, "x"), null);
  assertStrictEquals(optionalString({ x: 42 }, "x"), null);
  assertStrictEquals(optionalString({ x: " ok " }, "x"), "ok");
});

Deno.test("textParam: bounds the length so an oversized label is named, not truncated", () => {
  assertStrictEquals(textParam({ x: "abc" }, "x", 3), "abc");
  assertApiError(() => textParam({ x: "abcd" }, "x", 3), 400, '"x" is too long');
});

// ------------------------------------------------------ request: booleans

Deno.test("boolParam: accepts a real boolean and the two query-string spellings", () => {
  assertStrictEquals(boolParam({ x: true }, "x"), true);
  assertStrictEquals(boolParam({ x: false }, "x"), false);
  assertStrictEquals(boolParam({ x: "true" }, "x"), true);
  assertStrictEquals(boolParam({ x: "false" }, "x"), false);
});

Deno.test("boolParam: throws on anything else", () => {
  assertApiError(() => boolParam({}, "x"), 400, '"x" must be true or false.');
  assertApiError(() => boolParam({ x: "yes" }, "x"), 400, '"x" must be true or false.');
});

Deno.test("optionalBool: absent is null, but a wrong value is ALSO null rather than an error", () => {
  // Deliberately asymmetric with boolParam above, and that is worth pinning.
  // `setScheduleActive` always sends a real boolean, so a stray value here is
  // treated as "not mentioned" and the update simply does not touch the column.
  assertStrictEquals(optionalBool({}, "x"), null);
  assertStrictEquals(optionalBool({ x: "yes" }, "x"), null);
  assertStrictEquals(optionalBool({ x: "false" }, "x"), false);
});

// ------------------------------------------------------ request: integers

Deno.test("intParam: a missing value takes the fallback", () => {
  assertStrictEquals(intParam({}, "limit", 50, 1, 500), 50);
  assertStrictEquals(intParam({ limit: "" }, "limit", 50, 1, 500), 50);
});

Deno.test("intParam: a JSON number and a query string both work", () => {
  assertStrictEquals(intParam({ limit: 25 }, "limit", 50, 1, 500), 25);
  assertStrictEquals(intParam({ limit: "25" }, "limit", 50, 1, 500), 25);
});

Deno.test("intParam: CLAMPS out-of-range values instead of rejecting them", () => {
  // Not an oversight: a limit larger than the cap should give the cap, not an
  // error. Pinned so a future change to throwing is a deliberate one.
  assertStrictEquals(intParam({ limit: 9999 }, "limit", 50, 1, 500), 500);
  assertStrictEquals(intParam({ limit: -5 }, "limit", 50, 1, 500), 1);
});

Deno.test("intParam: truncates a fractional value", () => {
  assertStrictEquals(intParam({ limit: 25.9 }, "limit", 50, 1, 500), 25);
});

Deno.test("intParam: a non-number is named rather than sent to Postgres as NaN", () => {
  assertApiError(() => intParam({ limit: "abc" }, "limit", 50, 1, 500), 400, '"limit" must be a number.');
});

// ---------------------------------------------------------- request: uuid

Deno.test("uuidParam: accepts a valid id in any case", () => {
  assertStrictEquals(uuidParam({ id: VALID_UUID }, "id"), VALID_UUID);
  assertStrictEquals(uuidParam({ id: VALID_UUID.toUpperCase() }, "id"), VALID_UUID.toUpperCase());
});

Deno.test("uuidParam: rejects a malformed id HERE, so the user never sees Postgres's cast error", () => {
  assertApiError(() => uuidParam({ id: "not-a-uuid" }, "id"), 400, '"id" is not a valid id.');
  assertApiError(() => uuidParam({ id: "123" }, "id"), 400, '"id" is not a valid id.');
  // `alerts.id` is bigserial, so this is the exact value the alerts function
  // must NOT read with uuidParam -- see alerts/index.ts.
  assertApiError(() => uuidParam({ id: "7" }, "id"), 400, '"id" is not a valid id.');
});

// ---------------------------------------------------------- request: time

Deno.test("timeParam: normalises HH:MM and HH:MM:SS to the form Postgres stores", () => {
  assertStrictEquals(timeParam({ t: "9:05" }, "t"), "09:05:00");
  assertStrictEquals(timeParam({ t: "09:05" }, "t"), "09:05:00");
  assertStrictEquals(timeParam({ t: "09:05:30" }, "t"), "09:05:30");
  assertStrictEquals(timeParam({ t: "00:00" }, "t"), "00:00:00");
  assertStrictEquals(timeParam({ t: "23:59" }, "t"), "23:59:00");
});

Deno.test("timeParam: rejects an impossible clock time", () => {
  assertApiError(() => timeParam({ t: "24:00" }, "t"), 400, '"t" is not a valid time.');
  assertApiError(() => timeParam({ t: "12:60" }, "t"), 400, '"t" is not a valid time.');
});

Deno.test("timeParam: rejects a shape it cannot parse", () => {
  assertApiError(() => timeParam({ t: "noon" }, "t"), 400, '"t" must be a clock time');
  // One-digit minutes are not accepted: "9:5" is ambiguous between 09:05 and
  // 09:50, and guessing would silently schedule the wrong time.
  assertApiError(() => timeParam({ t: "9:5" }, "t"), 400, '"t" must be a clock time');
});

Deno.test("optionalTime: absent, null and empty all mean null", () => {
  assertStrictEquals(optionalTime({}, "t"), null);
  assertStrictEquals(optionalTime({ t: null }, "t"), null);
  assertStrictEquals(optionalTime({ t: "" }, "t"), null);
  assertStrictEquals(optionalTime({ t: "6:30" }, "t"), "06:30:00");
});

// ---------------------------------------------------------- request: days

Deno.test("daysParam: accepts the app's array form", () => {
  assertEquals(daysParam({ d: [0, 2, 4] }, "d"), [0, 2, 4]);
});

Deno.test("daysParam: accepts the Postgres literal a curl caller would paste", () => {
  assertEquals(daysParam({ d: "{0,2,4}" }, "d"), [0, 2, 4]);
  assertEquals(daysParam({ d: "[0,2,4]" }, "d"), [0, 2, 4]);
});

Deno.test("daysParam: canonicalises order and duplicates", () => {
  // The column CHECK accepts duplicates, but they are meaningless and would
  // make two equal schedules compare unequal.
  assertEquals(daysParam({ d: [4, 0, 2, 0] }, "d"), [0, 2, 4]);
});

Deno.test("daysParam: drops out-of-range days, and refuses when nothing valid is left", () => {
  assertEquals(daysParam({ d: [0, 9, -1, 6] }, "d"), [0, 6]);
  assertApiError(() => daysParam({ d: [7, 9] }, "d"), 400, "Pick at least one day");
  assertApiError(() => daysParam({ d: [] }, "d"), 400, "Pick at least one day");
});

Deno.test("daysParam: an absent value is named, not defaulted", () => {
  assertApiError(() => daysParam({}, "d"), 400, "The days of the week were not provided.");
});

Deno.test("daysParam: 0 is Monday and 6 is Sunday, matching the runner's engine", () => {
  // The app stores 0=Monday..6=Sunday (`schedule.dart`), so the boundary values
  // are the ones worth naming. A Sunday-only schedule is [6], never [7].
  assertEquals(daysParam({ d: [0] }, "d"), [0]);
  assertEquals(daysParam({ d: [6] }, "d"), [6]);
  // 7 is out of range, so a [7]-only request filters down to nothing and is
  // refused rather than stored as an empty (and therefore never-firing) week.
  assertApiError(() => daysParam({ d: [7] }, "d"), 400, "Pick at least one day");
});

// ---------------------------------------------------------- request: date

Deno.test("optionalDate: normalises to a bare YYYY-MM-DD", () => {
  assertStrictEquals(optionalDate({ d: "2026-09-26" }, "d"), "2026-09-26");
  // A full timestamp is trimmed to its date, which is what the `date` column holds.
  assertStrictEquals(optionalDate({ d: "2026-09-26T10:00:00Z" }, "d"), "2026-09-26");
  assertStrictEquals(optionalDate({}, "d"), null);
  assertStrictEquals(optionalDate({ d: "" }, "d"), null);
});

Deno.test("optionalDate: rejects a non-date shape", () => {
  assertApiError(() => optionalDate({ d: "26/09/2026" }, "d"), 400, '"d" must be a date');
  assertApiError(() => optionalDate({ d: "tomorrow" }, "d"), 400, '"d" must be a date');
});

Deno.test("optionalDate: KNOWN GAP -- the regex checks shape, not calendar validity", () => {
  // "2026-99-99" matches the pattern, so it passes through and Postgres is the
  // thing that rejects it -- as a cast error the user cannot act on, rather than
  // this module's sentence naming the field.
  //
  // Pinned rather than silently "fixed", because changing it changes what the
  // six functions accept, and that is a decision, not a detail. The app builds
  // its dates from a picker (`Schedule.toRow`), so it cannot produce this; only
  // a hand-written curl can.
  assertStrictEquals(optionalDate({ d: "2026-99-99" }, "d"), "2026-99-99");
});

// ------------------------------------------------------- parseRequest

function makeRequest(url: string, body?: string, method = "POST"): Request {
  return new Request(url, {
    method,
    body,
    headers: body === undefined ? undefined : { "content-type": "application/json" },
  });
}

Deno.test("parseRequest: reads a read-style call from the query string", async () => {
  const { op, params } = await parseRequest(makeRequest("https://x.test/jobs?op=list&limit=50", undefined, "GET"));
  assertStrictEquals(op, "list");
  assertStrictEquals(params["limit"], "50");
});

Deno.test("parseRequest: reads a write-style call from the JSON body", async () => {
  const { op, params } = await parseRequest(makeRequest("https://x.test/jobs", '{"op":"run_now","notebook_id":"abc"}'));
  assertStrictEquals(op, "run_now");
  assertStrictEquals(params["notebook_id"], "abc");
});

Deno.test("parseRequest: a body value wins over the same query parameter", async () => {
  // The documented rule: an explicit body value is more specific than a stray
  // query string, which matters because a POST from a browser can carry both.
  const { params } = await parseRequest(
    makeRequest("https://x.test/schedules?is_active=true", '{"op":"update","is_active":false}'),
  );
  assertStrictEquals(params["is_active"], false);
});

Deno.test("parseRequest: a missing or blank op is refused by name", async () => {
  await assertThrowsAsync(
    () => parseRequest(makeRequest("https://x.test/jobs", '{"limit":5}')),
    ApiError,
    'No operation was named',
  );
  await assertThrowsAsync(
    () => parseRequest(makeRequest("https://x.test/jobs?op=%20%20", undefined, "GET")),
    ApiError,
    'No operation was named',
  );
});

Deno.test("parseRequest: an empty body is fine, so ?op=health works with no payload", async () => {
  const { op } = await parseRequest(makeRequest("https://x.test/status?op=health", undefined, "GET"));
  assertStrictEquals(op, "health");
});

Deno.test("parseRequest: a body that is valid JSON but not an object is refused", async () => {
  await assertThrowsAsync(
    () => parseRequest(makeRequest("https://x.test/jobs", '["op","list"]')),
    ApiError,
    "wrong type",
  );
});

Deno.test("parseRequest: a body that is not JSON is refused with a readable sentence", async () => {
  await assertThrowsAsync(
    () => parseRequest(makeRequest("https://x.test/jobs", "op=list")),
    ApiError,
    "cannot be read",
  );
});

// `assertThrowsAsync` is small enough that importing another module for it is
// not worth it; keeping it here also makes the assertion shape obvious.
async function assertThrowsAsync(
  fn: () => Promise<unknown>,
  errorClass: new (...args: never[]) => Error,
  messageIncludes: string,
): Promise<void> {
  let caught: unknown;
  try {
    await fn();
  } catch (error) {
    caught = error;
  }
  assert(caught !== undefined, "expected the call to throw, but it resolved");
  assertInstanceOf(caught, errorClass);
  const message = (caught as Error).message;
  assert(
    message.includes(messageIncludes),
    `expected the message to mention ${JSON.stringify(messageIncludes)}, got ${JSON.stringify(message)}`,
  );
}

Deno.test("Params: a name that was never sent reads as undefined, not as a string 'undefined'", () => {
  const params: Params = { op: "list" };
  assertFalse("limit" in params);
  assertStrictEquals(params["limit"], undefined);
});