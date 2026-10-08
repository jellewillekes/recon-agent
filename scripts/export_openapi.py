#!/usr/bin/env python3
"""Write the API's OpenAPI schema to `web/openapi.json` (#121).

The web front end's typed client is generated from it (`npm run api:types`),
so an API contract change shows up as a TypeScript error in `web/`.
`tests/test_api_web.py` fails until the committed file matches the API.
"""

import argparse
import json
import sys
from pathlib import Path

DEFAULT_OUTPUT = Path("web/openapi.json")


def main(argv: list[str] | None = None) -> int:
    """Write the schema, sorted and indented so diffs stay readable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    from recon.api.main import app

    schema = json.dumps(app.openapi(), indent=2, sort_keys=True)
    args.output.write_text(schema + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
