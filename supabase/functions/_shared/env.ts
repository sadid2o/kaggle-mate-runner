// Environment access, in one place and verified against the docs.
//
// Two naming schemes exist and both must work:
//
//   * Hosted projects inject the *plural, named-key* variables as JSON objects:
//     `SUPABASE_SECRET_KEYS` / `SUPABASE_PUBLISHABLE_KEYS`, e.g.
//     `{"default":"sb_secret_..."}`. The documented read is
//     `JSON.parse(Deno.env.get('SUPABASE_SECRET_KEYS')!)['default']`.
//   * Local `supabase functions serve` and the runner's own GitHub secrets use
//     the singular `SUPABASE_SECRET_KEY` / `SUPABASE_PUBLISHABLE_KEY`.
//
// Reading both is not defensive padding: the same code is deployed to hosted
// and run locally, and silently getting `undefined` for the secret key would
// turn every request into a 401 with no clue why.
//
// Nothing here is hardcoded. The project URL and both keys come from the
// environment the platform provides.

import { ApiError } from "./http.ts";

function readKey(pluralName: string, singularName: string): string | null {
  const plural = Deno.env.get(pluralName);
  if (plural) {
    try {
      const parsed = JSON.parse(plural);
      // `default` is the name the platform documents for the auto-injected key.
      // Any other named key is accepted too, so a project that added one is not
      // forced to rename it.
      const value = parsed?.["default"] ?? Object.values(parsed ?? {})[0];
      if (typeof value === "string" && value.length > 0) return value;
    } catch (error) {
      console.error(`[km] ${pluralName} is not valid JSON: ${error}`);
    }
  }
  const singular = Deno.env.get(singularName);
  return singular && singular.length > 0 ? singular : null;
}

export function supabaseUrl(): string {
  const url = Deno.env.get("SUPABASE_URL");
  if (!url) {
    // Never reaches the client in a form that leaks anything: the string names
    // the missing variable but not its value.
    throw new ApiError(
      500,
      "The server is not configured correctly (SUPABASE_URL is missing).",
    );
  }
  return url;
}

/// The service key. Bypasses RLS, so it is used only server-side here and is
/// never placed in a response body.
export function secretKey(): string {
  const key = readKey("SUPABASE_SECRET_KEYS", "SUPABASE_SECRET_KEY");
  if (!key) {
    throw new ApiError(
      500,
      "The secret key is not set on the server, so data cannot be read.",
    );
  }
  return key;
}

/// The publishable key, read but **never used as proof of anything**.
///
/// There is no `requirePublishableKey` and there must not be one. The app's
/// `functions.invoke` carries no credential at all -- `FunctionsClient` applies
/// its auth headers to `auth`, `rest`, `storage` and `realtime` and not to
/// `functions` (the pinned source is quoted in `db.ts`) -- so a check that
/// demanded this key would reject every real request while proving nothing about
/// the caller. It is also embedded in a notebook the user may publish (0002), so
/// treating it as a secret would be wrong for a second reason.
///
/// Kept as an export because reading it correctly is still worth having in one
/// place: nothing in this tree calls it today, and a caller outside it (the
/// runner, which needs the key to arm the cancel channel) uses its own env var.
export function publishableKey(): string | null {
  return readKey("SUPABASE_PUBLISHABLE_KEYS", "SUPABASE_PUBLISHABLE_KEY");
}
