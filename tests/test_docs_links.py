"""Relative links in the repo's Markdown resolve to files that exist.

The docs, ADRs, README, CONTRIBUTING and AGENTS.md link to each other and to
source files. Renaming or moving one silently breaks the links into it. This
runs offline in `make check`; external links are checked weekly by
`.github/workflows/links.yml` instead.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
EXTERNAL = ("http://", "https://", "mailto:")

pytestmark = pytest.mark.unit


def _markdown_files() -> list[Path]:
    top_level = [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "AGENTS.md")]
    return top_level + sorted((ROOT / "docs").rglob("*.md"))


def broken_links(doc: Path) -> list[str]:
    """Relative link targets in `doc` that don't exist, outside code fences."""
    broken: list[str] = []
    in_fence = False
    for line in doc.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for target in LINK.findall(line):
            if target.startswith(EXTERNAL) or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if not (doc.parent / path).exists():
                broken.append(target)
    return broken


@pytest.mark.parametrize(
    "doc", _markdown_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_relative_links_resolve(doc: Path) -> None:
    assert broken_links(doc) == [], f"{doc.relative_to(ROOT)} has broken links"


def test_the_check_catches_a_broken_link_and_ignores_the_rest(tmp_path: Path) -> None:
    (tmp_path / "real.md").write_text("x\n")
    doc = tmp_path / "doc.md"
    doc.write_text(
        "[ok](real.md) [ok with anchor](real.md#part) [same page](#top)\n"
        "[external](https://example.com/missing) [broken](missing.md)\n"
        "```\n[inside a fence](also-missing.md)\n```\n"
    )
    assert broken_links(doc) == ["missing.md"]
