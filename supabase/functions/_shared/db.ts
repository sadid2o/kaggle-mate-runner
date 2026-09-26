// The service-key client, used by every function.
//
// Read this before assuming the functions are authenticated.
//
// The app calls these functions with `supabase_flutter`'s functions client. I
// read the pinned source (supabase 2.16.1 / functions_client 2.7.1) rather than
// trusting the docs' claim that "the Supabase client libraries automatically
// handle authorization", and the claim does not hold here:
//
//   * `FunctionsClient` is constructed as `_initFunctionsClient()` ->
//     `FunctionsClient(_functionsUrl, {...headers})`, and at that point
//     `headers` holds nothing but `X-Client-Info`. The `apikey` and
//     `Authorization` headers live in `_getAuthHeaders()`, which is applied to
//     `auth`, `rest`, `storage` and `realtime` -- but *not* to `functions`.
//   * `invoke` adds only `Content-Type`.
//
// So a request from this app carries no credential at all. Whatever the backend
// does, it cannot claim to have authenticated the caller, and the functions are
// therefore `verify_jwt = false` with no credential check of their own. The
// publishable key is not a capability here: it is embedded in a notebook that
// the user may publish (see 0002_cancel_channel.sql), so it must never be
// treated as proof of identity.
//
// The sole real control is the service key, which stays server-side. That is
// the same posture `docs/DB_SCHEMA.md` describes: nothing is reachable without
// it, and the app has none of it.

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

import { secretKey, supabaseUrl } from "./env.ts";

/// One client per request. Created lazily so `env` failures surface as a proper
/// 500 with an English message instead of a boot-time crash.
export function admin(): SupabaseClient {
  return createClient(supabaseUrl(), secretKey(), {
    auth: {
      // These are not user sessions. Persisting or refreshing a session would
      // try to read a token that does not exist and log a warning on every call.
      persistSession: false,
      autoRefreshToken: false,
      detectSessionInUrl: false,
    },
    global: {
      headers: { "X-Client-Info": "kaggle-mate-functions/1.0" },
    },
  });
}
