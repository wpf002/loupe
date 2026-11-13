import { existsSync } from "node:fs";
import { PrismaPg } from "@prisma/adapter-pg";
import { buildApp } from "./app.js";
import { anthropicGateway } from "./claude.js";
import { loadConfig } from "./config.js";
import { PrismaClient } from "./generated/prisma/client.js";
import { argon2Hasher } from "./passwords.js";
import { prismaStore } from "./store.js";

if (existsSync(".env")) process.loadEnvFile(".env");

const config = loadConfig(process.env);
const prisma = new PrismaClient({ adapter: new PrismaPg({ connectionString: config.databaseUrl }) });
const app = buildApp({
  store: prismaStore(prisma),
  claude: anthropicGateway(config.anthropicApiKey),
  hasher: argon2Hasher,
  monthlyTokenCap: config.monthlyTokenCap,
  sessionDays: config.sessionDays,
  trustedProxyHops: config.trustedProxyHops,
});

async function shutdown(signal: string): Promise<void> {
  app.log.info({ signal }, "shutting down");
  await app.close();
  await prisma.$disconnect();
  process.exit(0);
}

process.once("SIGTERM", () => void shutdown("SIGTERM"));
process.once("SIGINT", () => void shutdown("SIGINT"));

await app.listen({ port: config.port, host: config.host });
