# Deployment

`charts/recon-agent` runs unchanged on k3d and on a managed cluster (GKE, AKS, EKS) — this
page covers what actually changes between the two, not a full operator's guide.

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
Honeycomb, or similar) instead of self-hosting Tempo, once that instrumentation exists.

## Image size

`docker build -t recon-agent:dev -f docker/Dockerfile .` produces a **262MB** image
(measured via `docker inspect recon-agent:dev --format '{{.Size}}'`, arm64, this
machine) — under the 300MB target issue #15 names. That's despite `claude-agent-sdk`
bundling a native `claude` CLI binary inside its own wheel and `polars` (unused anywhere
in `src/recon`) both being present: the unpacked `.venv` is ~660MB, but that data
compresses well, so the distributable image lands well under it. See
`docs/adr/0011-container-base-image-and-size.md` for how this was measured, not just
assumed. `polars`/`pytest`/`ruff`-as-main-deps are still worth trimming via a
`pyproject.toml` optional-dependencies split as a follow-up — real cleanup, but not
required to hit the size target.
