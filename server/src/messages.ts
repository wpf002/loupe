import { z } from "zod";

export const ALLOWED_MODELS = ["claude-opus-5-5"] as const;
export const ALLOWED_BETAS = ["server-side-fallback-2026-07-01"] as const;
export const MAX_TOKENS_CAP = 16_000;

const bodySchema = z.looseObject({
  model: z.enum(ALLOWED_MODELS),
  max_tokens: z.number().int().positive().max(MAX_TOKENS_CAP),
  messages: z.array(z.unknown()).min(1),
  stream: z.literal(false).optional(),
});

const betasSchema = z.array(z.enum(ALLOWED_BETAS));

export type ProxyRequest = {
  body: z.infer<typeof bodySchema>;
  betas: Array<(typeof ALLOWED_BETAS)[number]>;
};

export type ParseResult = { ok: true; request: ProxyRequest } | { ok: false; message: string };

function splitBetaHeader(header: string | string[] | undefined): string[] {
  const joined = Array.isArray(header) ? header.join(",") : (header ?? "");
  return joined
    .split(",")
    .map((name) => name.trim())
    .filter((name) => name.length > 0);
}

/** Checks a /v1/messages request against the model, beta and max_tokens limits. */
export function parseProxyRequest(body: unknown, betaHeader: string | string[] | undefined): ParseResult {
  const parsedBody = bodySchema.safeParse(body);
  if (!parsedBody.success) {
    const fields = parsedBody.error.issues.map((issue) => issue.path.join(".") || "body");
    return { ok: false, message: `Invalid request fields: ${[...new Set(fields)].join(", ")}` };
  }
  const parsedBetas = betasSchema.safeParse(splitBetaHeader(betaHeader));
  if (!parsedBetas.success) {
    return { ok: false, message: "Beta not allowed" };
  }
  return { ok: true, request: { body: parsedBody.data, betas: parsedBetas.data } };
}
