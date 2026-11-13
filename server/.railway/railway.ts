import { defineRailway, postgres, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const Postgres = postgres("Postgres", { region: "us-west2" });
  Postgres.networking = { privateNetworkEndpoint: "postgres" };
  const postgresVolume = volume("postgres-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "us-west2", sizeMB: 50000 });
  const loupeServer = service("loupe-server", {
    build: { buildEnvironment: "V3", builder: "RAILPACK", buildCommand: "pnpm build" },
    deploy: {
      // Migrations run before the new code takes traffic. They must stay backward compatible.
      preDeployCommand: ["pnpm db:deploy"],
      startCommand: "pnpm start",
      healthcheckPath: "/health",
      healthcheckTimeout: 60,
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 5,
    },
    replicas: { "us-west2": 1 },
  });

  return project("loupe", {
    variables: { managed: false },
    resources: [loupeServer, Postgres, postgresVolume],
  });
});
