import { createContext, useContext } from "react";
import type { Role } from "../api/client";

// Cosmetic only: hides or disables controls the signed-in role can't use so
// nobody clicks a button that is guaranteed to 403. The server checks the
// role again on every state-changing request; nothing here is a security
// boundary.
const RANK: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };

export function roleAllows(role: Role | null | undefined, required: Role): boolean {
  // An unknown role (newer server, hand-edited row) gets the least: reads.
  const rank = role && role in RANK ? RANK[role] : -1;
  return rank >= RANK[required];
}

export const RoleContext = createContext<Role>("viewer");

export function useRole(): Role {
  return useContext(RoleContext);
}

/** True if the signed-in account may use controls that need `required`. */
export function useCan(required: Role): boolean {
  return roleAllows(useRole(), required);
}

/** Tooltip for a control the signed-in role can't use. */
export function needsRole(required: Role): string {
  return `Needs the ${required} role`;
}
