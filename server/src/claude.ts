import Anthropic from "@anthropic-ai/sdk";
import type { BetaMessage } from "@anthropic-ai/sdk/resources/beta/messages/messages.js";
import type { ProxyRequest } from "./messages.js";

const REQUEST_TIMEOUT_MS = 300_000;
const MAX_RETRIES = 3;

export type ClaudeReply = { message: BetaMessage; inputTokens: number; outputTokens: number };

export class ClaudeError extends Error {
  constructor(
    readonly status: number,
    readonly errorType: string,
  ) {
    super(`Claude API returned ${status}`);
  }
}

export type ClaudeGateway = {
  create(request: ProxyRequest): Promise<ClaudeReply>;
};

/** Cache writes and reads count as input toward the monthly cap. */
function countTokens(message: BetaMessage): { inputTokens: number; outputTokens: number } {
  const usage = message.usage;
  const cached = (usage.cache_creation_input_tokens ?? 0) + (usage.cache_read_input_tokens ?? 0);
  return { inputTokens: usage.input_tokens + cached, outputTokens: usage.output_tokens };
}

function errorType(error: InstanceType<typeof Anthropic.APIError>): string {
  const body = error.error;
  if (typeof body === "object" && body !== null && "error" in body) {
    const inner = body.error;
    if (typeof inner === "object" && inner !== null && "type" in inner && typeof inner.type === "string") {
      return inner.type;
    }
  }
  return "api_error";
}

export function anthropicGateway(apiKey: string): ClaudeGateway {
  const client = new Anthropic({ apiKey, timeout: REQUEST_TIMEOUT_MS, maxRetries: MAX_RETRIES });
  return {
    async create(request) {
      try {
        // The body passed validation in messages.ts; the SDK sends it unchanged.
        const message = (await client.beta.messages.create({
          ...request.body,
          betas: request.betas,
          stream: false,
        } as Anthropic.Beta.Messages.MessageCreateParamsNonStreaming)) as BetaMessage;
        return { message, ...countTokens(message) };
      } catch (error) {
        if (error instanceof Anthropic.APIError && typeof error.status === "number") {
          throw new ClaudeError(error.status, errorType(error));
        }
        throw error;
      }
    },
  };
}
