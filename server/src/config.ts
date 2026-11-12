import { z } from "zod";

const DEFAULT_PORT = 8080;
const DEFAULT_MONTHLY_TOKEN_CAP = 3_000_000;
const DEFAULT_SESSION_DAYS = 90;

const configSchema = z.object({
  DATABASE_URL: z.url({ protocol: /^postgres(ql)?$/ }),
  ANTHROPIC_API_KEY: z.string().min(1, "ANTHROPIC_API_KEY is required"),
  PORT: z.coerce.number().int().min(1).max(65535).default(DEFAULT_PORT),
  HOST: z.string().min(1).default("0.0.0.0"),
  MONTHLY_TOKEN_CAP: z.coerce.number().int().positive().default(DEFAULT_MONTHLY_TOKEN_CAP),
  SESSION_DAYS: z.coerce.number().int().positive().max(365).default(DEFAULT_SESSION_DAYS),
});

export type Config = {
  databaseUrl: string;
  anthropicApiKey: string;
  port: number;
  host: string;
  monthlyTokenCap: number;
  sessionDays: number;
};

/** Reads and validates the environment. Throws with every problem listed. */
export function loadConfig(env: NodeJS.ProcessEnv): Config {
  const parsed = configSchema.safeParse(env);
  if (!parsed.success) {
    const problems = parsed.error.issues.map((issue) => `${issue.path.join(".")}: ${issue.message}`);
    throw new Error(`Invalid config. ${problems.join("; ")}`);
  }
  const values = parsed.data;
  return {
    databaseUrl: values.DATABASE_URL,
    anthropicApiKey: values.ANTHROPIC_API_KEY,
    port: values.PORT,
    host: values.HOST,
    monthlyTokenCap: values.MONTHLY_TOKEN_CAP,
    sessionDays: values.SESSION_DAYS,
  };
}
