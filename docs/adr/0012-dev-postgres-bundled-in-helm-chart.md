# ADR 0012: Dev-only Postgres bundled in `charts/recon-agent`

Status: Accepted
Date: 2026-09-11

## Context

Issue #15's own verify recipe is `helm install recon charts/recon-agent` followed by
`kubectl wait --for=condition=ready pod -l app=recon-agent --timeout=120s`, with no
Postgres provisioned anywhere in between. `kubectl wait --for=condition=ready` is driven
by the pod's **readiness** probe, and the same issue requires that probe point at
`/readyz` — which genuinely checks Postgres reachability
(`api/health.py::check_postgres`, `docs/contracts.md` section 5: "readiness — MCP server
reachable AND Postgres reachable"). Without a database anywhere in the cluster, the pod
would never turn Ready and the `kubectl wait` step would time out — the ticket's own
verify recipe doesn't work as literally written.

Two ways to close the gap: extend the verify recipe itself to provision a Postgres before
`helm install` (via `docker compose`'s postgres joined onto k3d's docker network, or a
one-off `kubectl run`), keeping `charts/recon-agent` limited to exactly the API-shaped
resources the issue lists (deployment/service/configmap/secret); or make the chart
self-contained so the ticket's recipe works unmodified. Decided with the user directly:
bundle a small, dev-only Postgres in the chart itself, toggled off by default reasoning
(`postgresql.enabled`, default `true`) rather than requiring an extra manual step every
time someone runs the verify flow.

## Decision

`charts/recon-agent/templates/postgres-deployment.yaml` and `postgres-service.yaml`, both
guarded by `{{ if .Values.postgresql.enabled }}`: a single-replica Deployment running
`pgvector/pgvector:pg17` (matching `docker/compose.yaml`'s image) with `emptyDir` storage
— no PVC, data does not survive a pod restart, by design; this exists to make readiness
probes pass in a throwaway k3d cluster, not to persist anything. `templates/secret.yaml`
builds `DATABASE_URL` from `.Values.postgresql.*` when enabled, pointing at the bundled
Postgres's in-cluster service DNS name; when `postgresql.enabled=false`, it falls through
to `.Values.secret.databaseUrl` verbatim, for pointing at a real managed database.

This is not a new chart dependency (no Bitnami/other subchart added) — just two more
templates authored the same way as the API's own deployment/service, so it doesn't touch
`CLAUDE.md`'s dependency-approval gate at all.

## Consequences

- `helm install recon charts/recon-agent` alone (no external setup) satisfies the issue's
  verify recipe unmodified, in a throwaway k3d cluster.
- `docs/deployment.md` documents flipping `postgresql.enabled` off and setting
  `secret.databaseUrl` for a managed cluster — the bundled Postgres is explicitly k3d/dev
  only, never intended to run in production.
- Two more resources exist in every default `helm install`, including a first read of the
  chart's manifest list. Commented inline and in `docs/deployment.md` so this reads as a
  deliberate verify-flow fix, not scope creep into "the chart now manages a database."
