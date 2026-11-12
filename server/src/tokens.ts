import { createHash, randomBytes } from "node:crypto";

const TOKEN_BYTES = 32;

/** A new opaque session token. Only its hash is ever stored. */
export function newSessionToken(): string {
  return randomBytes(TOKEN_BYTES).toString("base64url");
}

export function hashSessionToken(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

/** Calendar month in UTC, the unit of the token cap. */
export function monthKey(moment: Date): string {
  return moment.toISOString().slice(0, 7);
}

const DAY_MS = 24 * 60 * 60 * 1000;

export function expiryFrom(moment: Date, days: number): Date {
  return new Date(moment.getTime() + days * DAY_MS);
}
