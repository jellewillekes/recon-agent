# CI and release

What runs where, and why model evaluations aren't part of it.

## Why LLM evaluations run locally

GitHub Actions has no model credentials here, and that's a deliberate boundary, not a
gap. The runtimes use the Agent SDK credit on a personal subscription, so an eval in
CI would spend that credit on every push. It would also need the subscription token in
a repository secret that any workflow could read. The review bots have a token for
their own action (`docs/github-agents.md`) and never run `recon.cli eval`.

So `recon.cli eval` runs locally, and its result is committed under `evals/results/`.
CI checks what can be checked without a model: that the prompts still match the
committed baseline (below), and later that the baseline clears `config/thresholds.yaml`.
That second check comes with the first real baseline; see the implementation plan.

## Checks on every PR

| Workflow | Job | Fails when |
| --- | --- | --- |
| `ci.yml` | Hygiene | a generated or ignored path is tracked |
| `ci.yml` | Lint, Typecheck, Unit Tests | `make check` would fail |
| `ci.yml` | Image build and scan | the image doesn't build, or Trivy finds a HIGH or CRITICAL vulnerability with a fix available |
| `ci.yml` | Helm lint | `helm lint charts/recon-agent` fails |
| `company-names.yml` | Company names | a tracked file, a commit message, or the PR title or body names a company on the denylist |
| `gitleaks.yml` | Secret Scan | a secret is committed |
| `codeql.yml`, `zizmor.yml` | | code or workflow security findings |
| `pr-title.yml` | | the title isn't a Conventional Commit |
| `links.yml` | | an external doc link is broken (weekly, not per PR) |

### Image scan

Trivy fails the job on HIGH and CRITICAL findings that have a fixed version, and
ignores unfixed ones (`--ignore-unfixed`). The Debian base image carries dozens of
HIGH CVEs with no Debian fix yet. Failing on those would keep CI red with nothing to
upgrade to. A finding with a fix is usually a Python package, and `uv lock
--upgrade-package <name>` clears it. Trivy downloads its vulnerability database on each
run, so a newly published advisory can turn the job red on a PR that didn't change any
dependency.

Trivy runs as a binary installed by `scripts/install_pinned_tool.sh`, which pins the
version and the archive's SHA-256. It doesn't use `aquasecurity/trivy-action`, whose
tags were moved to credential-stealing code in March 2026 (GHSA-69fq-xp46-6x23). Helm
is installed the same way, so the lint doesn't depend on the runner image's version.
To upgrade either, change the version and checksum there, and check the archive's
provenance first (`gh attestation verify <archive> --repo aquasecurity/trivy`).

### Company names

AGENTS.md forbids naming companies in code, commits, docs and PR text.
`scripts/check_hygiene.py` checks it, and prints only where a name is, never the
name itself. The denylist would contain the names, so it's never committed:

- In CI it's the `HYGIENE_DENYLIST` repository secret, one name per line. Dependabot
  and fork PRs can't read secrets, so for those a missing list is a notice. Everywhere
  else it fails the job.
- Locally it's the gitignored `.hygiene-denylist`. Without it, the pre-commit hooks
  print a notice and pass.

The list holds the companies the dataset's questions reference, as names only.
Tickers aren't on it, since many are ordinary words. Names match as whole words,
ignoring case, so a short name that is also an ordinary word (a Greek letter, say)
would flag unrelated text. Keep those in their longer form. To draft the list from the
reviewed tickers files `recon.cli edgar fetch` uses:

```bash
uv run python scripts/check_hygiene.py propose data/raw/sec_edgar/tickers*.txt
uv run python scripts/check_hygiene.py check --commits origin/main..HEAD
```

Review the draft by hand, then paste it into the secret.

## Pre-commit

`make install-hooks` installs both the `pre-commit` and `commit-msg` stages. Re-run it
after pulling this change, since earlier installs only had `pre-commit`.

- ruff lint and format, whitespace, YAML and TOML checks, large files
- gitleaks, at the same version as `gitleaks.yml`
- forbidden tracked paths
- `prompt-baseline`: fails when a file in `prompts/` no longer matches the
  `prompt_hashes` in `evals/baseline.json`. The baseline was scored with the old
  prompt, so the gate would compare against a different agent. Run a full eval and
  replace the baseline in the same PR, or revert the prompt. Until a baseline exists
  it prints a notice. CI runs the same check in the Lint job.
- company names in tracked files, and in the commit message being written

## Release

Pushing a `v*` tag runs `release.yaml`. It builds the image and applies the same Trivy
gate as CI, and pushes to `ghcr.io/<owner>/recon-agent:<tag>` only if the scan passes.
The job summary shows the image digest. Deploy by that digest rather than the tag, since
a tag can be moved and a digest can't. Before the push, Trivy writes a CycloneDX SBOM of
the scanned image, attached to the run as the `sbom-<tag>` artifact. If that step fails,
nothing is pushed.
