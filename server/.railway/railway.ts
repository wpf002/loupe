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

  // The web app keeps each account's index, previews and vectors here. About 34GB per 200k items.
  const loupeWebVolume = volume("loupe-web-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "us-west2", sizeMB: 100000 });
  const loupeWeb = service("loupe-web", {
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    deploy: {
      healthcheckPath: "/health",
      healthcheckTimeout: 120,
      restartPolicyType: "ON_FAILURE",
      restartPolicyMaxRetries: 5,
    },
    replicas: { "us-west2": 1 },
    volumeMounts: { "loupe-web-volume": { mountPath: "/data" } },
  });

  return project("loupe", {
    variables: { managed: false },
    resources: [loupeServer, Postgres, postgresVolume, loupeWeb, loupeWebVolume],
  });
});
