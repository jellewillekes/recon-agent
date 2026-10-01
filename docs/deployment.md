# Deployment

`charts/recon-agent` runs unchanged on k3d and on a managed cluster (GKE, AKS, EKS) — this
page covers what actually changes between the two, not a full operator's guide.

Checked: 2026-09-30. `helm lint charts/recon-agent` is clean on Helm v4.2.1. The k3d
walk-through below was not re-run.

## Local: k3d

```bash
k3d cluster create recon
docker build -t recon-agent:dev -f docker/Dockerfile .
k3d image import recon-agent:dev -c recon
helm install recon charts/recon-agent
kubectl wait --for=condition=ready pod -l app=recon-agent --timeout=120s
kubectl port-forward svc/recon-recon-agent 8000:8000
```

`values.yaml`'s defaults (`image.pullPolicy: IfNotPresent`, `postgresql.enabled: true`)
are tuned for exactly this flow: no registry, no external database. See
`docs/adr/0012-dev-postgres-bundled-in-helm-chart.md` for why a throwaway Postgres ships
inside the chart itself rather than requiring a manual setup step before `helm install`.

The bundled Postgres gets a random password on first install, stored in the release's
Secret as `POSTGRES_PASSWORD` and reused on every `helm upgrade`. Nothing credential-like
is committed. Read it back with
`kubectl get secret recon-recon-agent -o jsonpath='{.data.POSTGRES_PASSWORD}' | base64 -d`.

## Local: Docker Compose

```bash
cp docker/.env.example docker/.env   # then fill in the passwords
docker compose -f docker/compose.yaml up -d
```

`docker/.env` is gitignored, and compose refuses to start while `POSTGRES_PASSWORD` or
`GRAFANA_ADMIN_PASSWORD` is unset. Postgres only reads its password when it initializes
an empty data directory. After changing `POSTGRES_PASSWORD`, drop the old volume with
`docker compose -f docker/compose.yaml down -v`.

Every image is pinned to a version tag. A floating `:latest` Tempo image once broke
`docker/tempo.yaml` by rejecting a config block the previous schema accepted.

Open http://localhost:8000 for the local research UI. The page uses the
synthetic fixture data configured for Compose. To submit questions, set
CLAUDE_CODE_OAUTH_TOKEN in the untracked docker/.env file. The token stays in
the API container and is never sent to the browser.

## Tool data in containers

The image carries no SEC EDGAR cache, so the chart (`env.toolData`) and
`docker/compose.yaml` set `RECON_TOOL_DATA=fixture`. The MCP tools then answer from the
synthetic fixture, and `/readyz` passes. Serving real data from a container means
mounting a `data/processed/sec_edgar/<snapshot>/` directory built by
`recon.cli edgar fetch` and setting `RECON_TOOL_DATA=edgar`. With `edgar` and no
snapshot, the MCP server refuses to start and `/readyz` fails. That's deliberate: see
`docs/data-sources.md`.

## What changes for a managed cluster

### Ingress

k3d verify uses `kubectl port-forward` only — `charts/recon-agent` has no Ingress
resource (not in issue #15's template list, deliberately out of scope for this step). A
managed cluster needs one added: an `Ingress` resource plus TLS via `cert-manager`, or
the cluster's native load-balancer integration. Future work, not part of this chart yet.

### Secrets

Set `postgresql.enabled=false` and point `secret.databaseUrl` at a managed Postgres
instance with the `pgvector` extension enabled (Cloud SQL, RDS, or equivalent) — the
bundled dev Postgres (`docs/adr/0012`) is explicitly not for production; it has no
persistent storage and no backup story.

`secret.claudeCodeOauthToken` and `secret.databaseUrl` are plain Helm values today, fine
for a throwaway k3d cluster but not for a real one. Prefer an External Secrets Operator or
Sealed Secrets pulling from the cluster's actual secret manager (GCP Secret Manager, AWS
Secrets Manager) over `--set`-ing credentials directly — those still end up in Helm's
release history otherwise.

### Image registry

The k3d flow builds locally and `k3d image import`s straight into the cluster — no
registry involved. A managed cluster needs the image pushed to a real registry (GHCR,
matching `docs/github-agents.md`'s existing tooling) and `values.yaml`'s
`image.repository`/`image.tag`/`image.pullPolicy` switched to the registry image,
ideally pinned by digest rather than a mutable tag. Building and pushing that image is
step 11's job (`.github/workflows/release.yaml`), not this one.

### OTLP endpoint

`docker/compose.yaml`'s Grafana/Tempo/Prometheus/Ollama are dev-stack only — nothing in
`src/recon` exports OpenTelemetry traces yet (that instrumentation is step 12). A managed
cluster would point OTLP export at a managed observability backend (Grafana Cloud,
Honeycomb, or similar) instead of self-hosting Tempo (docs/observability.md).

## Image size

`docker build -t recon-agent:dev -f docker/Dockerfile .` produces a **206MB** image
(206,192,971 bytes after step 13 added the pgvector client; 204,420,718 before, via `docker inspect recon-agent:dev --format '{{.Size}}'`, arm64,
this machine), under the 300MB target issue #15 names. It was 262MB until #54 dropped
the unused `polars` and moved `pytest` and `ruff` to the dev group, which the image
doesn't install. `claude-agent-sdk` still bundles a native `claude` CLI binary inside
its own wheel, but that data compresses well. See
`docs/adr/0011-container-base-image-and-size.md` for how this was measured, not just
assumed. CI builds the image on every PR and scans it with Trivy (`docs/ci.md`).
