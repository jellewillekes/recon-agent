# ADR 0019: How CI scans the image and checks for company names

Status: Accepted
Date: 2026-09-30

## Context

Step 11 (#16) adds a Trivy scan of the image that fails on HIGH and CRITICAL findings,
and #76 adds an automated check for the "no company names" rule. Each had more than
one reasonable shape.

- **How to run Trivy.** `aquasecurity/trivy-action` is the usual route. In March 2026
  an attacker moved 76 of its 77 version tags to credential-stealing code
  (GHSA-69fq-xp46-6x23). A SHA pin would have protected this repo, but the action
  still downloads a Trivy binary at run time.
- **Which findings fail the job.** On the current image, Trivy reports 62 HIGH and
  CRITICAL findings with no fix available, almost all in the Debian base image. Only
  six had a fix, all in one Python package, and a lockfile bump cleared them.
- **What the name check matches.** The dataset's questions reference about 40
  companies. Their tickers include ordinary words, and so do some short company names.
  The list itself would contain the names, so it can't be committed.

## Decision

- Trivy and Helm run as binaries installed by `scripts/install_pinned_tool.sh`, which
  pins each version and the archive's SHA-256. The Trivy archive's provenance was
  checked with `gh attestation verify` before pinning. No new third-party action.
- The scan fails only on HIGH and CRITICAL findings with a fixed version
  (`--ignore-unfixed`). The user chose this over failing on all of them.
- The denylist holds company names only, not tickers, matched as whole words and
  ignoring case. The user chose this. Short names that are ordinary words stay in their
  longer form. The list lives in the `HYGIENE_DENYLIST` secret and a gitignored local
  file, and the check prints locations, never names.
- The name check is its own workflow, `company-names.yml`, not a job in `ci.yml`. It
  also runs on an edited PR title or body, and adding `edited` to `ci.yml` would re-run
  the whole suite on every description edit.

## Consequences

- A base-image CVE with no fix doesn't block a PR. It shows up in the scan output and
  fails the job once Debian ships a fix and the image isn't rebuilt on it.
- Upgrading Trivy or Helm means editing a version and a checksum in one script, not
  bumping an action. Dependabot doesn't cover it.
- A company missing from the denylist passes silently, and so does a name written in a
  form the list doesn't hold. The check backs up the rule, it doesn't replace it.
- Dependabot and fork PRs can't read the secret, so the check only prints a notice for
  them.
