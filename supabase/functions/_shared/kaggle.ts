// The two Kaggle calls the app needs: "is this token real?" and "what is this
// notebook's source right now?".
//
// Nothing here is guessed. Every value below was read out of the installed
// `kagglesdk` 0.1.37 and `kaggle` 2.2.4 packages in the runner's `.venv`, which
// is the exact pair `requirements.txt` pins and the exact pair the runner
// itself uses. The evidence for each piece:
//
//   * Base URL and path. `kagglesdk/kaggle_http_client.py::_get_request_url`
//     builds `{base}/v1/{service}/{request}` where, on prod, `base` is
//     `https://api.kaggle.com` (`kaggle_env.py::_env_to_endpoint`). Every call
//     is a POST with a JSON body (`_prepare_request`).
//
//   * Service and request names come from the generated client, not from me:
//     `KernelsApiClient.list_kernels` calls
//     `("kernels.KernelsApiService", "ListKernels", ...)` and `get_kernel` calls
//     `("kernels.KernelsApiService", "GetKernel", ...)`.
//
//   * Field names are the `json_name`s from each request's `_fields` table:
//     ListKernels sends `pageSize`, `group`, `user`, `language`, `kernelType`,
//     `outputType`, `sortBy`, `search`, `competition`, `dataset`, `page`,
//     `parentKernel`, `pageToken`; GetKernel sends `userName`, `kernelSlug`,
//     `versionLabel`. The enums are sent as their `.name` strings
//     (`EnumSerializer._to_str` returns `v.name`), which is why `group` is
//     "EVERYONE" and not 3.
//
//   * The paging defaults are copied from
//     `KaggleApi.kernels_list_with_response`, because "no page given" is not the
//     same as "page 1". `_set_paging` sets `page_token = ""` when no token was
//     passed, and `page = 1` only when there is also no token; that function
//     also sets `page_size`. A request that omits `page` entirely is a
//     different request from the one the CLI makes, so both are sent.
//
//   * The verify request is the exact one `kaggle kernels list -m --page-size 1`
//     makes -- `runner/kaggle_cli.py::verify` runs that command and treats a
//     non-zero exit as a bad credential, so `accounts verify` has to ask Kaggle
//     the same question. `kernels_list_with_response(mine=True)` is where the
//     CLI computes `group = "profile"`, which `lookup_enum` turns into
//     `KernelsListViewType.PROFILE` and `EnumSerializer._to_str` sends as the
//     name `"PROFILE"`.
//
//   * Errors: a body carrying `code >= 400` is surfaced by `_prepare_response`
//     as an HTTPError with `message` from the body, and anything else is judged
//     by `raise_for_status`. This module reads `message` when it is there for
//     the same reason -- it is Kaggle's own sentence about what went wrong.
//
// What is deliberately *not* used here: `ApiListKernelsRequest.user`. Passing
// the username turns the check into "this user has at least one notebook",
// which fails a perfectly good token on a brand-new account. `group=PROFILE` is
// what actually asks Kaggle to authorize the credential -- it returns only the
// caller's own notebooks -- so the request carries no `user` and leaves
// `pageSize` at the CLI's own value of 1.

import { upstream } from "./http.ts";

const KAGGLE_API_BASE = "https://api.kaggle.com";

/// The `json_name`s of `ApiListKernelsResponse.kernels[].ref`. Read from the
/// `ApiKernelMetadata._fields` table rather than assumed from the field name.
export interface KernelSummary {
  ref: string;
  title: string;
  slug: string;
  author: string;
  language: string | null;
  kernelType: string | null;
  lastRunTime: string | null;
  currentVersionNumber: number | null;
}

/// The 17 keys `KaggleApi.kernels_pull` writes into `kernel-metadata.json`.
///
/// The shape is not invented: `kaggle-mate-runner/runner/worker.py` reads this
/// file back with `parse_metadata` and takes `code_file` from it, and
/// `KaggleApi.kernels_pull` builds exactly these keys. Getting one wrong means
/// the runner pushes a folder whose `code_file` points at nothing.
export interface KernelMetadataFile {
  id: string;
  id_no: number;
  title: string;
  code_file: string;
  language: string;
  kernel_type: string;
  is_private: boolean;
  enable_gpu: boolean;
  enable_tpu: boolean;
  enable_internet: boolean;
  keywords: string[];
  dataset_sources: string[];
  kernel_sources: string[];
  competition_sources: string[];
  model_sources: string[];
  docker_image: string | null;
  machine_shape: string | null;
}

export interface PulledKernel {
  ref: string;
  version: number;
  /// The notebook's source, straight from `blob.source`.
  source: string;
  language: string;
  codeFile: string;
  metadata: KernelMetadataFile;
}

/// The one place a Kaggle credential is turned into an HTTP request.
///
/// Two credential shapes exist and they are not interchangeable, which is why
/// this branches rather than sending one header:
///
///   * A bare API token goes in `Authorization: Bearer <token>` --
///     `KaggleHttpClient.BearerAuth.__call__` sets exactly that, and
///     `_try_fill_auth` reaches it through `get_access_token_from_env()`, i.e.
///     the `KAGGLE_API_TOKEN` path `runner/kaggle_cli.py` uses for a token with
///     no username.
///   * A legacy `username:key` pair is HTTP Basic -- `_try_fill_auth` sets
///     `self._session.auth = (username, password)` when there is no API token,
///     and requests turns that into a Basic header. `runner/kaggle_cli.py`
///     builds exactly that pair from a `kaggle.json` blob.
///
/// The username is therefore required for Key and must be absent for Token,
/// which mirrors `KaggleCredentials.from_secret` on the runner side. A token
/// sent as Basic, or a key sent as Bearer, is rejected by Kaggle with a 401 that
/// looks like "your token is wrong" -- the least useful failure to debug.
function authHeader(username: string | null, credential: string): string {
  if (username) {
    // `btoa` is Latin-1; a username or key outside that range would encode
    // wrongly, so it is encoded through TextEncoder instead. Kaggle keys are
    // ASCII in practice, but silently corrupting a credential is not a failure
    // worth risking on an assumption.
    const bytes = new TextEncoder().encode(`${username}:${credential}`);
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return `Basic ${btoa(binary)}`;
  }
  return `Bearer ${credential}`;
}

/// POST one request to Kaggle and return the parsed JSON.
///
/// The error text is Kaggle's own `message` when the body carries one: it names
/// the actual problem ("Invalid credentials", "Not found"), and a generic
/// sentence here would throw that away.
async function callKaggle(
  serviceName: string,
  requestName: string,
  body: Record<string, unknown>,
  username: string | null,
  credential: string,
): Promise<Record<string, unknown>> {
  const url = `${KAGGLE_API_BASE}/v1/${serviceName}/${requestName}`;

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // The SDK's own default user agent, so this traffic is identifiable as
        // the Kaggle CLI family rather than an anonymous client.
        "User-Agent": "kaggle-api/v1.7.0",
        Authorization: authHeader(username, credential),
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(20_000),
    });
  } catch (error) {
    // A timeout or DNS failure. Distinct from a rejection: the token may be
    // perfectly good and Kaggle simply unreachable, and saying "wrong token"
    // then would send the user to fix something that is not broken.
    const detail = error instanceof Error ? error.message : String(error);
    throw upstream(`Could not reach Kaggle. Please try again. (${detail})`);
  }

  const text = await response.text();
  let payload: Record<string, unknown> = {};
  try {
    payload = text ? (JSON.parse(text) as Record<string, unknown>) : {};
  } catch {
    payload = {};
  }

  const code = typeof payload["code"] === "number" ? payload["code"] : null;
  if (code !== null && code >= 400) {
    const message = typeof payload["message"] === "string" ? payload["message"] : "";
    throw upstream(kaggleMessageToBengali(message, code));
  }
  if (!response.ok) {
    throw upstream(kaggleMessageToBengali("", response.status));
  }

  return payload;
}

/// Turn a Kaggle rejection into something the user can act on.
///
/// Deliberately blunt about the 401/403 case: `accounts verify` exists to
/// answer "is this token still good?", so when Kaggle says no, the sentence has
/// to say the token is the problem. Anything vaguer makes the button useless.
function kaggleMessageToBengali(message: string, code: number): string {
  if (code === 401 || code === 403) {
    return "Kaggle did not accept this token. The token may be expired or wrong — get a new token from Kaggle and add it again.";
  }
  if (code === 404) {
    return "This notebook was not found on Kaggle. Check that the owner/slug is correct, and if the notebook is private, use the account that owns it.";
  }
  if (message.trim()) {
    return `Kaggle says: ${message.trim()}`;
  }
  return `Kaggle rejected the request (${code}).`;
}

/// Ask Kaggle whether this credential is valid, and return the caller's own
/// notebooks as proof.
///
/// `group = PROFILE` (`KernelsListViewType.PROFILE`) is the whole trick and it
/// is a documented enum value, not an invented filter: it is what
/// `kernels_list(mine=True)` sends, and it returns only notebooks owned by the
/// authenticated caller. A request Kaggle cannot attribute to a real account
/// therefore comes back empty or rejected, which is exactly the question being
/// asked. An empty result is not treated as failure -- a real but brand-new
/// account legitimately owns nothing yet, and `accounts verify` failing there
/// would be a lie.
export async function listOwnKernels(
  username: string | null,
  credential: string,
): Promise<KernelSummary[]> {
  const payload = await callKaggle(
    "kernels.KernelsApiService",
    "ListKernels",
    // The CLI's own defaults for a one-item listing, so this request is
    // identical in shape to `kaggle kernels list -m --page-size 1` except for
    // the deliberately omitted `user`.
    {
      pageSize: 1,
      page: 1,
      pageToken: "",
      group: "PROFILE",
      sortBy: "HOTNESS",
      user: "",
      language: "all",
      kernelType: "all",
      outputType: "all",
      search: "",
      competition: "",
      dataset: "",
      parentKernel: "",
    },
    username,
    credential,
  );

  const kernels = payload["kernels"];
  if (!Array.isArray(kernels)) return [];

  return kernels.map((entry) => {
    const row = (entry ?? {}) as Record<string, unknown>;
    return {
      ref: asString(row["ref"]),
      title: asString(row["title"]),
      slug: asString(row["slug"]),
      author: asString(row["author"]),
      language: asStringOrNull(row["language"]),
      kernelType: asStringOrNull(row["kernelType"]),
      lastRunTime: asStringOrNull(row["lastRunTime"]),
      currentVersionNumber:
        typeof row["currentVersionNumber"] === "number" ? row["currentVersionNumber"] : null,
    } satisfies KernelSummary;
  });
}

/// Pull one notebook version: its source plus the metadata the runner pushes
/// back.
///
/// `versionLabel` is only sent when a specific version was asked for. Left
/// empty, Kaggle returns the latest -- which is what `notebooks pull` wants.
export async function getKernel(
  ref: string,
  versionLabel: string | null,
  username: string | null,
  credential: string,
): Promise<PulledKernel> {
  const { owner, slug } = splitRef(ref);

  const request: Record<string, unknown> = { userName: owner, kernelSlug: slug };
  if (versionLabel !== null) request["versionLabel"] = versionLabel;

  const payload = await callKaggle(
    "kernels.KernelsApiService",
    "GetKernel",
    request,
    username,
    credential,
  );

  const blob = (payload["blob"] ?? {}) as Record<string, unknown>;
  const metadata = (payload["metadata"] ?? {}) as Record<string, unknown>;

  const source = asString(blob["source"]);
  if (source.trim() === "") {
    // A version with no source is not a notebook the runner can push, and
    // storing it would fail much later as a missing file during the push.
    throw upstream("Kaggle did not return this notebook's source. Check whether the notebook can be opened on Kaggle.");
  }

  const language = asString(blob["language"]) || asString(metadata["language"]) || "python";
  const codeFile = codeFileName(metadata, language);
  const version = versionNumber(metadata);

  return {
    ref: asString(metadata["ref"]) || ref,
    version,
    source,
    language,
    codeFile,
    metadata: {
      id: asString(metadata["ref"]) || ref,
      id_no: typeof metadata["id"] === "number" ? metadata["id"] : 0,
      title: asString(metadata["title"]),
      code_file: codeFile,
      language,
      kernel_type: asString(metadata["kernelType"]) || asString(blob["kernelType"]) || "notebook",
      // These three are pushed back to Kaggle unchanged, so an absent value
      // must not become `null` in the file the runner reads -- the CLI's own
      // file always carries booleans.
      is_private: metadata["isPrivate"] === true,
      enable_gpu: metadata["enableGpu"] === true,
      enable_tpu: metadata["enableTpu"] === true,
      enable_internet: metadata["enableInternet"] === true,
      keywords: asStringArray(metadata["categoryIds"]),
      dataset_sources: asStringArray(metadata["datasetDataSources"]),
      kernel_sources: asStringArray(metadata["kernelDataSources"]),
      competition_sources: asStringArray(metadata["competitionDataSources"]),
      model_sources: asStringArray(metadata["modelDataSources"]),
      docker_image: asStringOrNull(metadata["dockerImage"]),
      machine_shape: asStringOrNull(metadata["machineShape"]),
    },
  };
}

/// `owner/slug`, with the error naming the fix.
///
/// Checked here rather than left to Kaggle, which would answer with a
/// "not found" that reads like the notebook is missing instead of the ref being
/// malformed. `kaggle_ref` is unique per account in the schema, so this is also
/// where a bare slug is caught before it becomes a duplicate-looking row.
export function splitRef(ref: string): { owner: string; slug: string } {
  const trimmed = ref.trim();
  const parts = trimmed.split("/");
  if (parts.length !== 2 || !parts[0] || !parts[1]) {
    throw upstream(`Kaggle reference "${trimmed}" is not valid. It must be in the form "owner/notebook-name".`);
  }
  return { owner: parts[0], slug: parts[1] };
}

/// Derive the notebook's file name the way Kaggle does.
///
/// Kaggle stores `owner/notebook-slug`, and a pull writes the code to
/// `<last part of ref>.ipynb`. This mirrors that so the `kernel-metadata.json`
/// this function stores matches the one `kaggle kernels pull` writes; the runner
/// then finds the file it expects in the folder it builds for the push.
function codeFileName(metadata: Record<string, unknown>, language: string): string {
  const ref = asString(metadata["ref"]);
  const slug = ref.includes("/") ? ref.split("/").pop() ?? "" : "";
  if (slug) return `${slug}.ipynb`;
  const title = asString(metadata["title"]);
  if (title) return `${title}.ipynb`;
  return language === "r" ? "notebook.r" : "notebook.ipynb";
}

/// The metadata's own version counter, which is what the schema's
/// `notebook_versions.version` holds.
///
/// `currentVersionNumber` is the field Kaggle documents for this, and it is
/// authoritative: numbering our own rows independently would let the stored
/// version disagree with Kaggle's after an out-of-band run, and `jobs` records
/// `kaggle_version_id`, so a wrong number is a wrong record of what ran.
function versionNumber(metadata: Record<string, unknown>): number {
  const current = metadata["currentVersionNumber"];
  if (typeof current === "number" && Number.isInteger(current) && current >= 1) {
    return current;
  }
  // Kaggle omitted it. 1 is the safe answer: it can only understate, and the
  // unique `(notebook_id, version)` constraint would otherwise be violated by a
  // guessed higher number that collides with a version already stored.
  return 1;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === "string");
}
