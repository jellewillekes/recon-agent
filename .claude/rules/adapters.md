---
paths:
  - "src/recon/adapters/**/*"
  - "config/sec_edgar.yaml"
---

# Adapters

- Map and validate only. Never interpret, compute or fill in a value the source doesn't carry (`docs/contracts.md`, ADR 0015).
- Every adapter records the source's license and attribution; see `docs/data-sources.md`.
- SEC EDGAR: stay under 10 requests/second, send the contact User-Agent from `SEC_EDGAR_USER_AGENT` only, and never write it to a file.
- Company lists are derived at fetch time into tickers files under the gitignored `data/`.
- Network access happens in the fetch step only. Normalizing and tests read cached files.
