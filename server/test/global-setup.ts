import { execFileSync } from "node:child_process";
import { userInfo } from "node:os";

export const TEST_DATABASE_URL =
  process.env["TEST_DATABASE_URL"] ?? `postgresql://${userInfo().username}@localhost:5432/loupe_test`;

export default function setup(): void {
  execFileSync("npx", ["prisma", "migrate", "deploy"], {
    env: { ...process.env, DATABASE_URL: TEST_DATABASE_URL },
    stdio: "pipe",
  });
}
