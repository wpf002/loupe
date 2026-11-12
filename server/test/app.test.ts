import { PrismaPg } from "@prisma/adapter-pg";
import type { FastifyInstance } from "fastify";
import { afterAll, beforeEach, describe, expect, it } from "vitest";
import { buildApp } from "../src/app.js";
import { ClaudeError, type ClaudeGateway } from "../src/claude.js";
import { loadConfig } from "../src/config.js";
import { PrismaClient } from "../src/generated/prisma/client.js";
import type { ProxyRequest } from "../src/messages.js";
import { argon2Hasher } from "../src/passwords.js";
import { prismaStore } from "../src/store.js";
import { hashSessionToken } from "../src/tokens.js";
import { TEST_DATABASE_URL } from "./global-setup.js";

const prisma = new PrismaClient({ adapter: new PrismaPg({ connectionString: TEST_DATABASE_URL }) });
const NOW = new Date("2026-11-12T12:00:00Z");
const PASSWORD = "correct horse battery";
const CAP = 1_000;

type FakeClaude = ClaudeGateway & { calls: ProxyRequest[]; failWith: ClaudeError | null };

function fakeClaude(): FakeClaude {
  const fake: FakeClaude = {
    calls: [],
    failWith: null,
    async create(request) {
      fake.calls.push(request);
      if (fake.failWith !== null) throw fake.failWith;
      return {
        message: { id: "msg_test", type: "message", role: "assistant", content: [] } as never,
        inputTokens: 300,
        outputTokens: 200,
      };
    },
  };
  return fake;
}

let claude: FakeClaude;
let app: FastifyInstance;

function validMessage(overrides: Record<string, unknown> = {}) {
  return {
    model: "claude-opus-5-5",
    max_tokens: 16_000,
    messages: [{ role: "user", content: "Six hooks" }],
    thinking: { type: "adaptive" },
    ...overrides,
  };
}

async function signUp(email = "owner@example.test"): Promise<string> {
  const response = await app.inject({ method: "POST", url: "/v1/accounts", payload: { email, password: PASSWORD } });
  expect(response.statusCode).toBe(201);
  return response.json<{ token: string }>().token;
}

function bearer(token: string) {
  return { authorization: `Bearer ${token}` };
}

beforeEach(async () => {
  await prisma.$executeRaw`TRUNCATE "Usage", "Session", "Account"`;
  claude = fakeClaude();
  app = buildApp({
    store: prismaStore(prisma),
    claude,
    hasher: argon2Hasher,
    monthlyTokenCap: CAP,
    sessionDays: 90,
    now: () => NOW,
    logger: false,
  });
});

afterAll(async () => {
  await prisma.$disconnect();
});

describe("accounts and sessions", () => {
  it("creates an account and stores only the token hash", async () => {
    const token = await signUp();
    const sessions = await prisma.session.findMany();
    expect(sessions).toHaveLength(1);
    expect(sessions[0]?.tokenHash).toBe(hashSessionToken(token));
    expect(sessions[0]?.tokenHash).not.toBe(token);
    const account = await prisma.account.findFirstOrThrow();
    expect(account.passwordHash.startsWith("$argon2id$")).toBe(true);
  });

  it("normalises email and refuses a duplicate", async () => {
    await signUp("Owner@Example.test");
    const again = await app.inject({
      method: "POST",
      url: "/v1/accounts",
      payload: { email: "owner@example.test", password: PASSWORD },
    });
    expect(again.statusCode).toBe(409);
  });

  it("refuses a short password", async () => {
    const response = await app.inject({
      method: "POST",
      url: "/v1/accounts",
      payload: { email: "owner@example.test", password: "short" },
    });
    expect(response.statusCode).toBe(400);
  });

  it("signs in with the right password only", async () => {
    await signUp();
    const wrong = await app.inject({
      method: "POST",
      url: "/v1/sessions",
      payload: { email: "owner@example.test", password: "wrong password here" },
    });
    expect(wrong.statusCode).toBe(401);
    const unknown = await app.inject({
      method: "POST",
      url: "/v1/sessions",
      payload: { email: "nobody@example.test", password: PASSWORD },
    });
    expect(unknown.statusCode).toBe(401);
    const right = await app.inject({
      method: "POST",
      url: "/v1/sessions",
      payload: { email: "owner@example.test", password: PASSWORD },
    });
    expect(right.statusCode).toBe(201);
  });

  it("signs out by deleting the session", async () => {
    const token = await signUp();
    expect((await app.inject({ method: "DELETE", url: "/v1/sessions", headers: bearer(token) })).statusCode).toBe(204);
    expect((await app.inject({ method: "GET", url: "/v1/me", headers: bearer(token) })).statusCode).toBe(401);
  });

  it("rejects an expired session", async () => {
    const token = await signUp();
    await prisma.session.updateMany({ data: { expiresAt: new Date("2026-11-01T00:00:00Z") } });
    expect((await app.inject({ method: "GET", url: "/v1/me", headers: bearer(token) })).statusCode).toBe(401);
  });
});

describe("messages proxy", () => {
  it("needs a session", async () => {
    const response = await app.inject({ method: "POST", url: "/v1/messages", payload: validMessage() });
    expect(response.statusCode).toBe(401);
    expect(response.json()).toEqual({
      type: "error",
      error: { type: "authentication_error", message: "Sign in again" },
    });
    expect(claude.calls).toHaveLength(0);
  });

  it("forwards a valid request with its allowed beta and counts tokens", async () => {
    const token = await signUp();
    const response = await app.inject({
      method: "POST",
      url: "/v1/messages?beta=true",
      headers: { ...bearer(token), "anthropic-beta": "server-side-fallback-2026-07-01" },
      payload: validMessage(),
    });
    expect(response.statusCode).toBe(200);
    expect(response.json().id).toBe("msg_test");
    expect(claude.calls[0]?.betas).toEqual(["server-side-fallback-2026-07-01"]);
    expect(claude.calls[0]?.body.thinking).toEqual({ type: "adaptive" });
    const me = await app.inject({ method: "GET", url: "/v1/me", headers: bearer(token) });
    expect(me.json()).toEqual({ email: "owner@example.test", month: "2026-11", used: 500, cap: CAP });
  });

  it.each([
    ["another model", validMessage({ model: "claude-other" })],
    ["max_tokens over the cap", validMessage({ max_tokens: 64_000 })],
    ["streaming", validMessage({ stream: true })],
    ["no messages", validMessage({ messages: [] })],
  ])("rejects %s", async (_label, payload) => {
    const token = await signUp();
    const response = await app.inject({ method: "POST", url: "/v1/messages", headers: bearer(token), payload });
    expect(response.statusCode).toBe(400);
    expect(claude.calls).toHaveLength(0);
  });

  it("rejects a beta outside the allowlist", async () => {
    const token = await signUp();
    const response = await app.inject({
      method: "POST",
      url: "/v1/messages",
      headers: { ...bearer(token), "anthropic-beta": "some-other-beta" },
      payload: validMessage(),
    });
    expect(response.statusCode).toBe(400);
  });

  it("stops at the monthly cap without retries", async () => {
    const token = await signUp();
    const headers = bearer(token);
    for (let call = 0; call < 2; call += 1) {
      expect((await app.inject({ method: "POST", url: "/v1/messages", headers, payload: validMessage() })).statusCode).toBe(200);
    }
    const capped = await app.inject({ method: "POST", url: "/v1/messages", headers, payload: validMessage() });
    expect(capped.statusCode).toBe(429);
    expect(capped.headers["x-should-retry"]).toBe("false");
    expect(claude.calls).toHaveLength(2);
  });

  it("passes the Claude status through without the body", async () => {
    const token = await signUp();
    claude.failWith = new ClaudeError(529, "overloaded_error");
    const response = await app.inject({ method: "POST", url: "/v1/messages", headers: bearer(token), payload: validMessage() });
    expect(response.statusCode).toBe(529);
    expect(response.json().error.type).toBe("overloaded_error");
  });
});

describe("health and config", () => {
  it("reports health", async () => {
    const response = await app.inject({ method: "GET", url: "/health" });
    expect(response.json()).toEqual({ ok: true });
  });

  it("fails fast on bad config", () => {
    expect(() => loadConfig({ DATABASE_URL: "nope" })).toThrow(/DATABASE_URL.*ANTHROPIC_API_KEY/);
    const config = loadConfig({ DATABASE_URL: "postgresql://localhost/loupe", ANTHROPIC_API_KEY: "test" });
    expect(config.port).toBe(8080);
    expect(config.monthlyTokenCap).toBe(3_000_000);
  });
});
