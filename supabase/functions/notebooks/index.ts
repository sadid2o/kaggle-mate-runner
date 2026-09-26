// `notebooks` — the Kaggle notebooks the user tracks, and their pulled source.
//
// This is the function that writes to Storage. `add` and `pull` both end with a
// `notebook_versions` row and the two objects that row points at, and the
// runner depends on all three: `worker.py::start` does
// `storage_download("notebooks", version["code_path"])`, reads `code_file` out
// of `meta_path`, and pushes the folder to Kaggle. A pull that stored a row
// without the objects would not fail here -- it would fail hours later, on the
// first scheduled run, as a missing file.
//
// The bucket keys are the same ones `docs/DB_SCHEMA.md` documents
// (`notebooks/{notebook_id}/v{n}/source` and `.../metadata`), and they are
// bucket-relative because that is what `storage_download` expects.
//
// Authorization is the same everywhere in this codebase: no credential arrives
// from the app, so there is none to check, and every read and write goes
// through the service key which never leaves the server. What protects the
// stored token is that it is decrypted only inside the `pull` path, only to
// hand it to Kaggle, and never included in a response.

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
import { getKernel, splitRef } from "../_shared/kaggle.ts";
import {
  optionalBool,
  optionalString,
  type Params,
  parseRequest,
  textParam,
  uuidParam,
} from "../_shared/request.ts";

const NOTEBOOK_COLUMNS =
  "id,account_id,kaggle_ref,display_name,is_active,last_seen_at,created_at";

const VERSION_COLUMNS =
  "id,notebook_id,version,code_path,meta_path,code_file,language,pulled_at";

const MAX_DISPLAY_NAME = 120;
const MAX_REF = 200;

/// Where the runner reads a version's source and metadata from.
///
/// Kept in one function because two writers (`add` and `pull`) must agree on it
/// exactly -- a mismatch would not be a visible bug, just a run that fails to
/// find its own files.
export function versionPaths(notebookId: string, version: number): { code: string; meta: string } {
  return {
    code: `${notebookId}/v${version}/source`,
    meta: `${notebookId}/v${version}/metadata`,
  };
}

export async function handleNotebooks(op: string, params: Params): Promise<Response> {
  const db = admin();

  switch (op) {
    case "list":
      return listNotebooks(db, params);
    case "add":
      return addNotebook(db, params);
    case "versions":
      return listVersions(db, params);
    case "pull":
      return pullVersion(db, params);
    case "update":
      return updateNotebook(db, params);
    case "delete":
      return deleteNotebook(db, params);
    default:
      throw badRequest(
        `"${op}" is not a valid operation. notebooks supports: list, add, versions, pull, update, delete.`,
      );
  }
}

/// List notebooks, with the two fields the app reads that are **not columns**.
///
/// `Notebook.fromJson` reads `version_count` and `latest_version`, and neither
/// exists in the schema -- the version history lives in `notebook_versions` and
/// is counted per notebook. A plain `select *` therefore renders every notebook
/// as "0 versions", which is wrong in a way nobody would trace back here, so
/// both are synthesised.
async function listNotebooks(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const accountId = optionalString(params, "account_id");

  let query = db.from("notebooks").select(NOTEBOOK_COLUMNS).order("created_at", { ascending: false });
  if (accountId !== null) query = query.eq("account_id", accountId);

  const { data, error } = await query;
  if (error) throw upstream(`could not fetch the notebook list: ${error.message}`);

  const rows = (data ?? []) as Array<Record<string, unknown>>;
  if (rows.length === 0) return itemsResponse([]);

  // One query for every notebook's versions rather than one per notebook: the
  // list is small, and N+1 round trips here would be paid on every app refresh.
  const ids = rows.map((row) => String(row["id"]));
  const versions = await db
    .from("notebook_versions")
    .select("notebook_id,version")
    .in("notebook_id", ids);

  if (versions.error) throw upstream(`could not fetch notebook versions: ${versions.error.message}`);

  const counts = new Map<string, number>();
  const latest = new Map<string, number>();
  for (const row of (versions.data ?? []) as Array<{ notebook_id: string; version: number }>) {
    counts.set(row.notebook_id, (counts.get(row.notebook_id) ?? 0) + 1);
    const best = latest.get(row.notebook_id) ?? 0;
    if (row.version > best) latest.set(row.notebook_id, row.version);
  }

  return itemsResponse(
    rows.map((row) => ({
      ...row,
      version_count: counts.get(String(row["id"])) ?? 0,
      // `latest_version` stays absent when nothing has been pulled, because the
      // model reads it as nullable and a 0 would read as "version 0 exists".
      latest_version: latest.get(String(row["id"])) ?? null,
    })),
  );
}

async function addNotebook(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const accountId = uuidParam(params, "account_id");
  const rawRef = textParam(params, "kaggle_ref", MAX_REF);
  const displayName = optionalString(params, "display_name");

  // Parsed here so a malformed ref is answered with the correct format rather
  // than stored and only discovered by `pull` below.
  const { owner, slug } = splitRef(rawRef);
  const ref = `${owner}/${slug}`;

  const account = await db
    .from("accounts")
    .select("id,kaggle_username,is_active,vault_secret_id")
    .eq("id", accountId)
    .maybeSingle();

  if (account.error) throw upstream(`could not look up the account: ${account.error.message}`);
  if (!account.data) throw notFound("This account no longer exists. Reload the list.");

  const existing = await db
    .from("notebooks")
    .select("id,display_name")
    .eq("account_id", accountId)
    .eq("kaggle_ref", ref)
    .maybeSingle();

  if (existing.error) throw upstream(`could not look up the notebook: ${existing.error.message}`);
  if (existing.data) {
    throw conflict(`"${ref}" is already added to this account ("${existing.data.display_name}").`);
  }

  const inserted = await db
    .from("notebooks")
    .insert({
      account_id: accountId,
      kaggle_ref: ref,
      // The app sends a display name only when the user typed one; falling back
      // to the Kaggle slug keeps every list readable without a second call.
      display_name: displayName ?? slug,
    })
    .select(NOTEBOOK_COLUMNS)
    .single();

  if (inserted.error || !inserted.data) {
    throw upstream(`could not save the notebook: ${inserted.error?.message ?? "unknown error"}`);
  }

  const notebook = inserted.data as Record<string, unknown>;
  const notebookId = String(notebook["id"]);

  // Pull version 1 immediately, exactly as the app's `addNotebook` documents:
  // the user finds out the pull failed while they are still on that screen,
  // rather than when the first scheduled run does. If the pull fails the
  // notebook row is removed again -- a notebook with no version can never run,
  // so leaving it would be a row that only ever produces failures.
  let version: Record<string, unknown>;
  try {
    version = await pullIntoStorage(db, notebookId, account.data, ref, null);
  } catch (error) {
    await db.from("notebooks").delete().eq("id", notebookId);
    throw error;
  }

  return itemResponse({
    ...notebook,
    version_count: 1,
    latest_version: version["version"],
  });
}

async function listVersions(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const notebookId = uuidParam(params, "notebook_id");

  const { data, error } = await db
    .from("notebook_versions")
    .select(VERSION_COLUMNS)
    .eq("notebook_id", notebookId)
    // Highest first: the app shows the newest pull at the top of the history.
    .order("version", { ascending: false });

  if (error) throw upstream(`could not fetch the version list: ${error.message}`);
  return itemsResponse(data ?? []);
}

async function pullVersion(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const notebookId = uuidParam(params, "notebook_id");

  const notebook = await db
    .from("notebooks")
    .select("id,account_id,kaggle_ref")
    .eq("id", notebookId)
    .maybeSingle();

  if (notebook.error) throw upstream(`could not look up the notebook: ${notebook.error.message}`);
  if (!notebook.data) throw notFound("This notebook no longer exists. Reload the list.");

  const account = await db
    .from("accounts")
    .select("id,kaggle_username,is_active,vault_secret_id")
    .eq("id", notebook.data.account_id)
    .maybeSingle();

  if (account.error) throw upstream(`could not look up the account: ${account.error.message}`);
  if (!account.data) throw notFound("This notebook's account no longer exists.");

  const version = await pullIntoStorage(
    db,
    notebookId,
    account.data,
    notebook.data.kaggle_ref,
    // A pull always takes the newest Kaggle version, which is what a pull
    // after an edit is for. Kaggle's own version number is used below rather
    // than an increment of our count, so the two cannot drift apart.
    null,
  );

  // `last_seen_at` is the record that a pull proved the notebook still exists
  // on Kaggle, which is the only evidence of that the system ever gets.
  await db.from("notebooks").update({ last_seen_at: new Date().toISOString() }).eq("id", notebookId);

  return itemResponse(version);
}

/// Fetch a notebook version from Kaggle and store it: source, metadata, row.
///
/// Order matters. The objects are written before the row, because the row is
/// what makes the version visible to the runner and to the app -- a row without
/// its objects is a version that looks present and fails at run time.
async function pullIntoStorage(
  db: ReturnType<typeof admin>,
  notebookId: string,
  account: Record<string, unknown>,
  ref: string,
  versionLabel: string | null,
): Promise<Record<string, unknown>> {
  const credential = await readCredential(db, account);

  const pulled = await getKernel(ref, versionLabel, credential.username, credential.secret);
  const paths = versionPaths(notebookId, pulled.version);

  // `kernel-metadata.json` is stored as the exact JSON the runner will read
  // back, so the runner never has to reconstruct it and a mismatch between the
  // two sides is impossible.
  const metaText = JSON.stringify(pulled.metadata, null, 2);

  const codeUpload = await db.storage
    .from("notebooks")
    .upload(paths.code, new Blob([pulled.source], { type: "text/plain; charset=utf-8" }), {
      // Overwriting is correct here: re-pulling the same Kaggle version should
      // refresh the stored source, not fail because the key is taken.
      upsert: true,
      contentType: "text/plain; charset=utf-8",
    });

  if (codeUpload.error) {
    throw upstream(`could not save the notebook source: ${codeUpload.error.message}`);
  }

  const metaUpload = await db.storage
    .from("notebooks")
    .upload(paths.meta, new Blob([metaText], { type: "application/json" }), {
      upsert: true,
      contentType: "application/json",
    });

  if (metaUpload.error) {
    throw upstream(`could not save the notebook metadata: ${metaUpload.error.message}`);
  }

  const inserted = await db
    .from("notebook_versions")
    .insert({
      notebook_id: notebookId,
      version: pulled.version,
      code_path: paths.code,
      meta_path: paths.meta,
      code_file: pulled.codeFile,
      language: pulled.language,
    })
    .select(VERSION_COLUMNS)
    .single();

  if (inserted.error || !inserted.data) {
    // `unique (notebook_id, version)` rejected this, which means the same
    // Kaggle version was already stored. That is a conflict worth naming rather
    // than a server fault, since the fix is "nothing to do, it is already here".
    if (inserted.error?.code === "23505") {
      throw conflict(
        `Kaggle version ${pulled.version} has already been pulled. Run a new version on Kaggle, then try again.`,
      );
    }
    throw upstream(`could not save the version: ${inserted.error?.message ?? "unknown error"}`);
  }

  return inserted.data as Record<string, unknown>;
}

/// Decrypt one account's credential, in the only shape the runner understands.
///
/// The read goes through `km_get_account_secret` -- the same function the runner
/// calls -- so the Edge Function and the runner cannot disagree about how a
/// token is spelled. `from_secret` on the runner side accepts a bare token, a
/// `username:key` pair and a `kaggle.json` blob, and so does this.
async function readCredential(
  db: ReturnType<typeof admin>,
  account: Record<string, unknown>,
): Promise<{ username: string | null; secret: string }> {
  const accountId = String(account["id"]);

  if (!account["vault_secret_id"]) {
    throw conflict("This account has no saved token. Add the token again.");
  }

  const result = await db.rpc("km_get_account_secret", { p_account_id: accountId });
  if (result.error) throw upstream(`could not read the token: ${result.error.message}`);

  const rows = Array.isArray(result.data) ? result.data : [];
  if (rows.length === 0) {
    if (account["is_active"] === false) {
      throw conflict("This account is turned off. Turn it on and try again.");
    }
    throw conflict("Could not find a token for this account. Add the token again.");
  }

  const row = rows[0] as { kaggle_username?: string; secret?: string };
  const raw = row.secret ?? "";
  const storedUsername = row.kaggle_username ?? (account["kaggle_username"] as string | null) ?? null;

  if (raw.startsWith("{")) {
    try {
      const data = JSON.parse(raw) as { username?: unknown; key?: unknown };
      if (typeof data.key === "string" && data.key !== "") {
        return {
          username: typeof data.username === "string" ? data.username : null,
          secret: data.key,
        };
      }
    } catch {
      throw conflict("The token for this account cannot be read (it is not JSON). Add the token again.");
    }
  }

  if (raw.includes(":")) {
    const [user, key] = raw.split(":", 2);
    if (user.trim() && key.trim()) return { username: user.trim(), secret: key.trim() };
  }

  return { username: null, secret: raw };
}

async function updateNotebook(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");
  const displayName = optionalString(params, "display_name");
  const isActive = optionalBool(params, "is_active");

  const patch: Record<string, unknown> = {};
  if (displayName !== null) {
    if (displayName.length > MAX_DISPLAY_NAME) {
      throw badRequest(`"display_name" is too long (${MAX_DISPLAY_NAME} characters max).`);
    }
    patch["display_name"] = displayName;
  }
  if (isActive !== null) patch["is_active"] = isActive;

  if (Object.keys(patch).length === 0) {
    throw badRequest("Nothing was given to change (display_name or is_active).");
  }

  const updated = await db
    .from("notebooks")
    .update(patch)
    .eq("id", id)
    .select(NOTEBOOK_COLUMNS)
    .maybeSingle();

  if (updated.error) throw upstream(`could not update the notebook: ${updated.error.message}`);
  if (!updated.data) throw notFound("This notebook no longer exists. Reload the list.");

  // The version count is returned so the updated row does not read as
  // version-less in the UI right after an edit.
  const count = await db
    .from("notebook_versions")
    .select("id", { count: "exact", head: true })
    .eq("notebook_id", id);

  return itemResponse({
    ...(updated.data as Record<string, unknown>),
    version_count: count.count ?? 0,
  });
}

async function deleteNotebook(db: ReturnType<typeof admin>, params: Params): Promise<Response> {
  const id = uuidParam(params, "id");

  // Storage objects are deleted with the row. The FK cascade removes the
  // `notebook_versions` rows, which is exactly the moment their `code_path` and
  // `meta_path` become unrecoverable -- so they have to be collected first, or
  // the bucket accumulates source code nothing points at.
  const versions = await db
    .from("notebook_versions")
    .select("code_path,meta_path")
    .eq("notebook_id", id);

  if (versions.error) throw upstream(`could not look up the notebook's versions: ${versions.error.message}`);

  const keys: string[] = [];
  for (const row of (versions.data ?? []) as Array<{ code_path: string; meta_path: string }>) {
    if (row.code_path) keys.push(row.code_path);
    if (row.meta_path) keys.push(row.meta_path);
  }

  const deleted = await db.from("notebooks").delete().eq("id", id).select("id").maybeSingle();
  if (deleted.error) throw upstream(`could not delete the notebook: ${deleted.error.message}`);
  if (!deleted.data) throw notFound("This notebook no longer exists. Reload the list.");

  if (keys.length > 0) {
    const removed = await db.storage.from("notebooks").remove(keys);
    if (removed.error) {
      // The row is already gone, which is what the user asked for. Reporting
      // this as a failure would be the wrong story; it is logged for the
      // operator, who is the only one who can clean the bucket.
      console.error(`[km] orphaned notebook objects after delete: ${removed.error.message}`);
    }
  }

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
    return await handleNotebooks(op, params);
  } catch (error) {
    return errorResponse(error);
  }
});
