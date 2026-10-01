#!/usr/bin/env bash
# Install a CI tool at a pinned version, verified against a pinned SHA-256.
#
#   scripts/install_pinned_tool.sh trivy|helm <dest-dir>
#
# The checksums live here, not in a file downloaded next to the archive: a
# checksum from the same release proves nothing if the release is replaced.
# Trivy runs as this binary rather than through aquasecurity/trivy-action,
# whose tags were moved to credential-stealing code in March 2026
# (GHSA-69fq-xp46-6x23). v0.74.0's archive was checked with
# `gh attestation verify --repo aquasecurity/trivy` before pinning.
set -euo pipefail

tool="${1:?usage: $0 trivy|helm <dest-dir>}"
dest="${2:?usage: $0 trivy|helm <dest-dir>}"

case "${tool}" in
  trivy)
    version="0.74.0"
    url="https://github.com/aquasecurity/trivy/releases/download/v${version}/trivy_${version}_Linux-64bit.tar.gz"
    sha256="2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a"
    member="trivy"
    ;;
  helm)
    version="4.2.1"
    url="https://get.helm.sh/helm-v${version}-linux-amd64.tar.gz"
    sha256="479dca836e5b45e8bd222400c5591b0e3a647378f03ff96597180db97c17fdae"
    member="linux-amd64/helm"
    ;;
  *)
    echo "Unknown tool '${tool}'. Use trivy or helm." >&2
    exit 2
    ;;
esac

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

curl -fsSL --retry 3 -o "${work}/archive.tar.gz" "${url}"
echo "${sha256}  ${work}/archive.tar.gz" | sha256sum -c --quiet -
tar -xzf "${work}/archive.tar.gz" -C "${work}" "${member}"
mkdir -p "${dest}"
install -m 0755 "${work}/${member}" "${dest}/${tool}"
"${dest}/${tool}" version >/dev/null
echo "${tool} ${version} installed to ${dest}"
