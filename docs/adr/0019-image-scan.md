# ADR 0019: How CI scans the image

Status: Accepted
Date: 2026-09-30

## Context

Step 11 (#16) adds a Trivy scan of the image that fails on HIGH and CRITICAL findings.
Two choices had more than one reasonable answer.

- **How to run Trivy.** `aquasecurity/trivy-action` is the usual route. In March 2026
  an attacker moved 76 of its 77 version tags to credential-stealing code
  (GHSA-69fq-xp46-6x23). A SHA pin would have protected this repo, but the action
  still downloads a Trivy binary at run time.
- **Which findings fail the job.** On the current image, Trivy reports 62 HIGH and
  CRITICAL findings with no fix available, almost all in the Debian base image. Only
  six had a fix, all in one Python package, and a lockfile bump cleared them.

## Decision

- Trivy and Helm run as binaries installed by `scripts/install_pinned_tool.sh`, which
  pins each version and the archive's SHA-256. The Trivy archive's provenance was
  checked with `gh attestation verify` before pinning. No new third-party action.
- The scan fails only on HIGH and CRITICAL findings with a fixed version
  (`--ignore-unfixed`). The user chose this over failing on all of them.

## Consequences

- A base-image CVE with no fix doesn't block a PR. It shows up in the scan output and
  fails the job once Debian ships a fix and the image isn't rebuilt on it.
- A newly published advisory can fail a PR that changed no dependency. The fix is a
  lockfile bump of the affected package.
- Upgrading Trivy or Helm means editing a version and a checksum in one script, not
  bumping an action. Dependabot doesn't cover it.
