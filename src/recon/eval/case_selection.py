"""Choose which cases an evaluation runs, so a run spends only what it needs.

Every case costs Agent SDK credit (AGENTS.md, Cost). A fixed subset, such as
`evals/smoke-cases.txt`, keeps a baseline cheap. The promotion gate only
compares runs over the same case set, so a subset baseline is compared with
runs over the same subset (docs/adr/0018-run-comparability-in-the-gate.md).
"""

from pathlib import Path

from recon.contracts import Case


def read_case_file(path: Path) -> list[str]:
    """Case ids from `path`, one per line. Blank lines and `#` comments are skipped."""
    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


def select_cases(cases: list[Case], case_ids: list[str]) -> list[Case]:
    """The cases with these ids, in dataset order. Unknown ids are an error."""
    wanted = set(case_ids)
    unknown = wanted - {case.case_id for case in cases}
    if unknown:
        raise ValueError(
            f"{len(unknown)} case id(s) aren't in the dataset, e.g. "
            f"{min(unknown)!r}. Check the case file against the dataset "
            "pin in recon.adapters.finance_agent_bench."
        )
    return [case for case in cases if case.case_id in wanted]


def spread_across_companies(
    cases: list[Case], case_ids_by_ticker: dict[str, list[str]], count: int
) -> list[str]:
    """`count` case ids, each about a different company.

    Deterministic, and blind to scores: walk the cases in case-id order and
    take each one that concerns exactly one company not yet taken. Questions
    comparing several companies are skipped, so each pick stands for one.
    """
    tickers_of: dict[str, list[str]] = {}
    for ticker, ids in case_ids_by_ticker.items():
        for case_id in ids:
            tickers_of.setdefault(case_id, []).append(ticker)
    picked: list[str] = []
    taken: set[str] = set()
    for case_id in sorted(case.case_id for case in cases):
        tickers = tickers_of.get(case_id, [])
        if len(tickers) == 1 and tickers[0] not in taken:
            picked.append(case_id)
            taken.add(tickers[0])
        if len(picked) == count:
            return picked
    raise ValueError(
        f"Only {len(picked)} single-company cases with distinct companies, "
        f"{count} asked for. Lower the count or review the tickers files."
    )


def with_tags(cases: list[Case], tags: list[str]) -> list[Case]:
    """The cases carrying any of `tags`, in their order. A tag no case carries
    is an error, since a typo would otherwise quietly narrow the selection."""
    known = {tag for case in cases for tag in case.tags}
    unknown = sorted(set(tags) - known)
    if unknown:
        raise ValueError(
            f"No case is tagged {unknown[0]!r}. Known tags: {', '.join(sorted(known))}."
        )
    wanted = set(tags)
    return [case for case in cases if wanted & set(case.tags)]
