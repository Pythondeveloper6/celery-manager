import { createContext, useContext } from "react";
import { DEFAULT_AUTH_OPTIONS, type AccountSource, type AuthOptions } from "../api/client";

// How this deployment signs people in (GET /api/auth/options), fetched once
// alongside whoami. Read by the pieces that change when the host app signs
// people in: sign-out, change password, and the Users page's labelling.
export const AuthOptionsContext = createContext<AuthOptions>(DEFAULT_AUTH_OPTIONS);

export function useAuthOptions(): AuthOptions {
  return useContext(AuthOptionsContext);
}

// Where the signed-in account comes from. Cosmetic like the role: the server
// refuses a password change for an external account on its own.
export const SourceContext = createContext<AccountSource>("local");

export function useAccountSource(): AccountSource {
  return useContext(SourceContext);
}
