import type { PrismaClient } from "./generated/prisma/client.js";

export type AccountRecord = { id: string; email: string; passwordHash: string };
export type SessionRecord = { accountId: string; email: string; expiresAt: Date };

export type Store = {
  ping(): Promise<void>;
  createAccount(email: string, passwordHash: string): Promise<string | null>;
  findAccount(email: string): Promise<AccountRecord | null>;
  createSession(tokenHash: string, accountId: string, expiresAt: Date): Promise<void>;
  findSession(tokenHash: string): Promise<SessionRecord | null>;
  deleteSession(tokenHash: string): Promise<void>;
  monthTokens(accountId: string, month: string): Promise<number>;
  addTokens(accountId: string, month: string, input: number, output: number): Promise<void>;
};

const UNIQUE_VIOLATION = "P2002";

function isUniqueViolation(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && error.code === UNIQUE_VIOLATION;
}

export function prismaStore(prisma: PrismaClient): Store {
  return {
    async ping() {
      await prisma.$queryRaw`SELECT 1`;
    },
    async createAccount(email, passwordHash) {
      try {
        const account = await prisma.account.create({ data: { email, passwordHash } });
        return account.id;
      } catch (error) {
        if (isUniqueViolation(error)) return null;
        throw error;
      }
    },
    findAccount(email) {
      return prisma.account.findUnique({
        where: { email },
        select: { id: true, email: true, passwordHash: true },
      });
    },
    async createSession(tokenHash, accountId, expiresAt) {
      await prisma.session.create({ data: { tokenHash, accountId, expiresAt } });
    },
    async findSession(tokenHash) {
      const session = await prisma.session.findUnique({
        where: { tokenHash },
        select: { accountId: true, expiresAt: true, account: { select: { email: true } } },
      });
      if (session === null) return null;
      return { accountId: session.accountId, email: session.account.email, expiresAt: session.expiresAt };
    },
    async deleteSession(tokenHash) {
      await prisma.session.deleteMany({ where: { tokenHash } });
    },
    async monthTokens(accountId, month) {
      const usage = await prisma.usage.findUnique({ where: { accountId_month: { accountId, month } } });
      return usage === null ? 0 : usage.inputTokens + usage.outputTokens;
    },
    async addTokens(accountId, month, input, output) {
      await prisma.usage.upsert({
        where: { accountId_month: { accountId, month } },
        create: { accountId, month, inputTokens: input, outputTokens: output },
        update: { inputTokens: { increment: input }, outputTokens: { increment: output } },
      });
    },
  };
}
