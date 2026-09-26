// `accounts` — the Kaggle credentials the runner uses, one row per account.
//
// The token lifecycle is the whole point of this function, and it is asymmetric
// on purpose:
//
//   * Writing is **create** — the token goes straight into Vault and the row
//     keeps only the returned secret id. The token is never echoed back and
//     never logged. `Account` on the app side has no field that could hold one,
//     which is what makes that guarantee checkable rather than a promise.
//   * Reading is **never** done here. There is no op that returns a token, and
//     `km_get_account_secret` (0001) is reachable only with the secret key, so a
//     stolen phone cannot read one back out of the API.
//
// Authorization: these functions carry no credential from the app (see
// `_shared/db.ts`), so nothing here inspects a header -- the boundary is that
// every write goes through the service key, which never leaves the server, and
// the publishable key reaches only `km_cancel_poll`. `verify` is the one op that
// touches an external system, and it is the only place a stored token is
// decrypted; it does not return the token either.

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
import { listOwnKernels } from "../_shared/kaggle.ts";
import {
  optionalBool,
  optionalString,
  type Params,
  parseRequest,
  stringParam,
  textParam,
  uuidParam,
} from "../_shared/request.ts";

/// The columns the app's `Account.fromJson` reads. `vault_secret_id` is
/// included because the model parses it, though it is an opaque Vault id and
/// not a credential.
const ACCOUNT_COLUMNS =
  "id,label,kaggle_username,vault_secret_id,is_active,last_verified_at,last_error,created_at";

/// A label is shown in lists and in job rows, so it is kept short.
const MAX_LABEL = 60;

/// The longest Kaggle credential worth storing. Real keys are far shorter; the
/// cap exists so a pasted blob cannot become a multi-megabyte Vault secret.
const MAX_TOKEN = 4096;

export async function handleAccounts(op: string, params: Params): Promise<Response> {
  const db = admin();

  switch (op) {
    case "list":
      return listAccounts(db, params);

    case "create":
      return createAccount(db, params);

    case "verify":
      return verifyAccount(db, params);

    case "update":
      return updateAccount(db, params);

    case "delete":
      return deleteAccount(db, params);

    default:
      throw badRequest(`"${op}" is not a valid operation. accounts supports: list, create, verify, update, delete.`);
  }
}

async function listAccounts(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  // Newest first, because a just-added account is the one the user is looking
  // for. `is_active` is deliberately not filtered: a deactivated account still
  // has to appear so it can be switched back on.
  const { data, error } = await db
    .from("accounts")
    .select(ACCOUNT_COLUMNS)
    .order("created_at", { ascending: false });

  if (error) throw upstream(`could not fetch the account list: ${error.message}`);
  return itemsResponse(data ?? []);
}

async function createAccount(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const label = textParam(params, "label", MAX_LABEL);
  const kaggleUsername = textParam(params, "kaggle_username", 128);
  const token = stringParam(params, "token");

  if (token.length > MAX_TOKEN) {
    throw badRequest("The token is unusually long. Paste the token you got from Kaggle.");
  }

  // Checked before writing: the schema has `unique (kaggle_username)`, and
  // letting the insert hit it would surface a constraint name instead of a
  // sentence naming the account that already exists.
  const existing = await db
    .from("accounts")
    .select("id,label")
    .eq("kaggle_username", kaggleUsername)
    .maybeSingle();

  if (existing.error) throw upstream(`could not look up the account: ${existing.error.message}`);
  if (existing.data) {
    throw conflict(
      `The account "${kaggleUsername}" is already added ("${existing.data.label}"). To use a new token, delete the old one and add it again.`,
    );
  }

  // The token is normalized the way the runner reads it back, so a token that
  // verifies here cannot fail there. `KaggleCredentials.from_secret` accepts a
  // bare token, a `username:key` pair, or a `kaggle.json` blob; a pair whose
  // prefix already matches the row keeps working as a token-only credential,
  // and anything else is stored as given.
  const credential = normalizeCredential(token, kaggleUsername);

  // Vault is not reachable over PostgREST, so the write goes through the
  // security-definer wrapper created in 0003. Only the secret id comes back.
  const created = await db.rpc("km_vault_create_secret", {
    p_secret: credential,
    // The name is for a human reading the Vault table, and it contains no part
    // of the secret.
    p_name: `kaggle:${kaggleUsername}`,
    p_description: `Kaggle API credential for account ${label}`,
  });

  if (created.error) throw upstream(`could not save the token: ${created.error.message}`);
  const secretId = created.data as unknown as string;

  const inserted = await db
    .from("accounts")
    .insert({
      label,
      kaggle_username: kaggleUsername,
      vault_secret_id: secretId,
    })
    .select(ACCOUNT_COLUMNS)
    .single();

  if (inserted.error || !inserted.data) {
    // Compensating delete. Without it a failed insert leaves an encrypted
    // secret in Vault with nothing pointing at it -- invisible, permanent, and
    // exactly the kind of thing that makes a Vault table untrustworthy.
    await db.rpc("km_vault_delete_secret", { p_id: secretId });
    throw upstream(`could not save the account: ${inserted.error?.message ?? "unknown error"}`);
  }

  return itemResponse(inserted.data);
}

/// Keep a stored credential in the shape the runner can actually consume.
///
/// Only the `username:` prefix that matches the row is stripped, because that
/// pair is the one `from_secret` will re-split into the same username. A
/// mismatched prefix is left untouched rather than quietly rewritten: rewriting
/// it would hide a real mistake (two accounts' credentials swapped) behind a
/// credential that happens to work.
function normalizeCredential(token: string, kaggleUsername: string): string {
  const prefix = `${kaggleUsername}:`;
  if (token.startsWith(prefix)) return token.slice(prefix.length);
  return token;
}

async function verifyAccount(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");

  const found = await db
    .from("accounts")
    .select("id,kaggle_username,is_active,vault_secret_id")
    .eq("id", id)
    .maybeSingle();

  if (found.error) throw upstream(`could not look up the account: ${found.error.message}`);
  if (!found.data) throw notFound("This account no longer exists. Reload the list.");
  if (!found.data.vault_secret_id) {
    throw conflict("This account has no saved token, so it cannot be verified. Add the token again.");
  }

  // The same RPC the runner uses, so verification exercises the read path the
  // runs depend on rather than a parallel one that could drift.
  const secret = await db.rpc("km_get_account_secret", { p_account_id: id });
  if (secret.error) throw upstream(`could not read the token: ${secret.error.message}`);

  const rows = Array.isArray(secret.data) ? secret.data : [];
  if (rows.length === 0) {
    // `km_get_account_secret` filters `is_active`, so this is exactly the
    // deactivated-account case, and saying so is more useful than "no secret".
    if (found.data.is_active === false) {
      throw conflict("This account is turned off. Turn it on and verify again.");
    }
    throw conflict("Could not find a token for this account. Add the token again.");
  }

  const row = rows[0] as { kaggle_username?: string; secret?: string };
  const credential = row.secret ?? "";
  // Mirrors the runner's own rule: the stored username wins, the pair is
  // re-interpreted exactly as `KaggleCredentials.from_secret` would.
  const username = row.kaggle_username ?? found.data.kaggle_username ?? null;
  const parsed = credential.startsWith("{") ? parseKaggleJson(credential) : null;
  const effectiveUsername = parsed ? parsed.username : username;
  const effectiveSecret = parsed ? parsed.key : credential;

  let failure: string | null = null;
  try {
    await listOwnKernels(effectiveUsername, effectiveSecret);
  } catch (error) {
    failure = error instanceof Error ? error.message : String(error);
  }

  // `last_error` is a column, not just a response field: the home screen shows
  // a warning banner for any account with a non-empty `last_error`
  // (`Account.needsAttention`), so clearing it on success is what makes the
  // banner go away after a fixed token.
  const patch = failure === null
    ? { last_verified_at: new Date().toISOString(), last_error: null }
    : { last_error: failure };

  const updated = await db
    .from("accounts")
    .update(patch)
    .eq("id", id)
    .select(ACCOUNT_COLUMNS)
    .single();

  if (updated.error || !updated.data) {
    throw upstream(`could not save the verification result: ${updated.error?.message ?? "unknown error"}`);
  }

  if (failure !== null) {
    // The row is returned alongside the error status by way of the message:
    // the failure reason is already stored in `last_error`, so the sentence the
    // user reads and the row the UI re-renders cannot disagree.
    throw upstream(failure);
  }

  return itemResponse(updated.data);
}

/// A `kaggle.json` blob, if that is what was stored.
function parseKaggleJson(raw: string): { username: string | null; key: string } | null {
  try {
    const data = JSON.parse(raw) as { username?: unknown; key?: unknown };
    if (typeof data.key !== "string" || data.key === "") return null;
    return {
      username: typeof data.username === "string" ? data.username : null,
      key: data.key,
    };
  } catch {
    return null;
  }
}

async function updateAccount(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");
  const label = optionalString(params, "label");
  const isActive = optionalBool(params, "is_active");

  const patch: Record<string, unknown> = {};
  if (label !== null) {
    if (label.length > MAX_LABEL) {
      throw badRequest(`"label" is too long (${MAX_LABEL} characters max).`);
    }
    patch["label"] = label;
  }
  if (isActive !== null) patch["is_active"] = isActive;

  if (Object.keys(patch).length === 0) {
    throw badRequest("Nothing was given to change (label or is_active).");
  }

  const updated = await db
    .from("accounts")
    .update(patch)
    .eq("id", id)
    .select(ACCOUNT_COLUMNS)
    .maybeSingle();

  if (updated.error) throw upstream(`could not update the account: ${updated.error.message}`);
  if (!updated.data) throw notFound("This account no longer exists. Reload the list.");
  return itemResponse(updated.data);
}

async function deleteAccount(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");

  // One function, not a row delete plus a Vault delete. Deleting the row alone
  // leaves the encrypted secret behind forever; deleting the secret alone
  // leaves an account whose every run fails. The migration does both in one
  // transaction so neither half-state is reachable.
  const result = await db.rpc("km_delete_account", { p_account_id: id });
  if (result.error) throw upstream(`could not delete the account: ${result.error.message}`);
  if (result.data !== true) throw notFound("This account no longer exists. Reload the list.");

  // The app's delete is optimistic and ignores the body, but an empty body
  // would look like a failure to `_one`, so an acknowledgement is sent.
  return okResponse();
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
    return await handleAccounts(op, params);
  } catch (error) {
    return errorResponse(error);
  }
});
