import rateLimit from "@fastify/rate-limit";
import Fastify, { type FastifyError, type FastifyInstance, type FastifyReply, type FastifyRequest } from "fastify";
import { z } from "zod";
import { ClaudeError, type ClaudeGateway } from "./claude.js";
import { parseProxyRequest } from "./messages.js";
import type { PasswordHasher } from "./passwords.js";
import type { SessionRecord, Store } from "./store.js";
import { expiryFrom, hashSessionToken, monthKey, newSessionToken } from "./tokens.js";

const MESSAGE_BODY_LIMIT_BYTES = 32 * 1024 * 1024;
const DEFAULT_BODY_LIMIT_BYTES = 64 * 1024;
const GLOBAL_RATE_PER_MINUTE = 120;
const AUTH_RATE_PER_MINUTE = 10;
const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_LENGTH = 256;

export type AppDeps = {
  store: Store;
  claude: ClaudeGateway;
  hasher: PasswordHasher;
  monthlyTokenCap: number;
  sessionDays: number;
  /** Proxy hops in front of the server. Railway's edge is 1. Request IPs key the rate limits. */
  trustedProxyHops?: number;
  now?: () => Date;
  logger?: boolean;
};

const credentialsSchema = z.object({
  email: z.email().max(320).transform((email) => email.trim().toLowerCase()),
  password: z.string().min(MIN_PASSWORD_LENGTH).max(MAX_PASSWORD_LENGTH),
});

/** Anthropic-shaped error body, so the Python SDK raises its usual error classes. */
function apiError(reply: FastifyReply, status: number, type: string, message: string): FastifyReply {
  return reply.code(status).send({ type: "error", error: { type, message } });
}

function bearerToken(request: FastifyRequest): string | null {
  const header = request.headers.authorization;
  if (header === undefined || !header.startsWith("Bearer ")) return null;
  const token = header.slice("Bearer ".length).trim();
  return token.length > 0 ? token : null;
}

export function buildApp(deps: AppDeps): FastifyInstance {
  const now = deps.now ?? (() => new Date());
  const proxyHops = deps.trustedProxyHops ?? 0;
  const app = Fastify({
    bodyLimit: DEFAULT_BODY_LIMIT_BYTES,
    // Trust only the nearest proxies, so a client can't spoof its IP with its own X-Forwarded-For.
    trustProxy: (_address: string, hop: number) => hop < proxyHops,
    logger:
      deps.logger === false
        ? false
        : {
            // Method, path and status only. No headers, bodies, query strings or addresses.
            serializers: {
              req: (request) => ({ method: request.method, path: request.url.split("?")[0] }),
              res: (response) => ({ statusCode: response.statusCode }),
            },
          },
  });

  let dummyHash: Promise<string> | null = null;
  const timingHash = () => (dummyHash ??= deps.hasher.hash("loupe-timing-equaliser"));

  async function issueSession(accountId: string): Promise<string> {
    const token = newSessionToken();
    await deps.store.createSession(hashSessionToken(token), accountId, expiryFrom(now(), deps.sessionDays));
    return token;
  }

  async function authenticate(request: FastifyRequest): Promise<SessionRecord | null> {
    const token = bearerToken(request);
    if (token === null) return null;
    const session = await deps.store.findSession(hashSessionToken(token));
    if (session === null || session.expiresAt <= now()) return null;
    return session;
  }

  app.register(rateLimit, { max: GLOBAL_RATE_PER_MINUTE, timeWindow: "1 minute" });

  app.setErrorHandler((error: FastifyError, request, reply) => {
    const status = typeof error.statusCode === "number" ? error.statusCode : 500;
    if (status >= 500) request.log.error({ code: error.code }, "request failed");
    const type = status === 429 ? "rate_limit_error" : status >= 500 ? "api_error" : "invalid_request_error";
    return apiError(reply, status, type, status >= 500 ? "Server error" : error.message);
  });

  app.register(async (routes) => {
    routes.get("/health", async (_request, reply) => {
      await deps.store.ping();
      return reply.send({ ok: true });
    });

    const authLimit = { config: { rateLimit: { max: AUTH_RATE_PER_MINUTE, timeWindow: "1 minute" } } };

    routes.post("/v1/accounts", authLimit, async (request, reply) => {
      const parsed = credentialsSchema.safeParse(request.body);
      if (!parsed.success) {
        return apiError(reply, 400, "invalid_request_error", `Email and a password of ${MIN_PASSWORD_LENGTH}+ characters`);
      }
      const passwordHash = await deps.hasher.hash(parsed.data.password);
      const accountId = await deps.store.createAccount(parsed.data.email, passwordHash);
      if (accountId === null) {
        return apiError(reply, 409, "invalid_request_error", "Account exists");
      }
      request.log.info({ accountId }, "account created");
      return reply.code(201).send({ token: await issueSession(accountId), email: parsed.data.email });
    });

    routes.post("/v1/sessions", authLimit, async (request, reply) => {
      const parsed = credentialsSchema.safeParse(request.body);
      if (!parsed.success) {
        return apiError(reply, 401, "authentication_error", "Wrong email or password");
      }
      const account = await deps.store.findAccount(parsed.data.email);
      const valid = await deps.hasher.verify(account?.passwordHash ?? (await timingHash()), parsed.data.password);
      if (account === null || !valid) {
        return apiError(reply, 401, "authentication_error", "Wrong email or password");
      }
      request.log.info({ accountId: account.id }, "session created");
      return reply.code(201).send({ token: await issueSession(account.id), email: account.email });
    });

    routes.delete("/v1/sessions", async (request, reply) => {
      const token = bearerToken(request);
      if (token !== null) await deps.store.deleteSession(hashSessionToken(token));
      return reply.code(204).send();
    });

    routes.get("/v1/me", async (request, reply) => {
      const session = await authenticate(request);
      if (session === null) return apiError(reply, 401, "authentication_error", "Sign in again");
      const month = monthKey(now());
      const used = await deps.store.monthTokens(session.accountId, month);
      return reply.send({ email: session.email, month, used, cap: deps.monthlyTokenCap });
    });

    routes.post("/v1/messages", { bodyLimit: MESSAGE_BODY_LIMIT_BYTES }, async (request, reply) => {
      const session = await authenticate(request);
      if (session === null) return apiError(reply, 401, "authentication_error", "Sign in again");
      const parsed = parseProxyRequest(request.body, request.headers["anthropic-beta"]);
      if (!parsed.ok) return apiError(reply, 400, "invalid_request_error", parsed.message);

      const month = monthKey(now());
      const used = await deps.store.monthTokens(session.accountId, month);
      if (used >= deps.monthlyTokenCap) {
        reply.header("x-should-retry", "false");
        return apiError(reply, 429, "rate_limit_error", "Monthly limit reached");
      }

      try {
        const result = await deps.claude.create(parsed.request);
        await deps.store.addTokens(session.accountId, month, result.inputTokens, result.outputTokens);
        request.log.info(
          { accountId: session.accountId, inputTokens: result.inputTokens, outputTokens: result.outputTokens },
          "claude call",
        );
        return reply.send(result.message);
      } catch (error) {
        if (error instanceof ClaudeError) {
          request.log.warn({ accountId: session.accountId, status: error.status }, "claude call failed");
          return apiError(reply, error.status, error.errorType, "Claude request failed");
        }
        throw error;
      }
    });
  });

  return app;
}
