"""Judge calls that fail during an evaluation run (#123).

A failed judge call ends that case's judging, not the run. The SDK raises
`ProcessError` when the CLI dies, and its subclass `ResultError` when the CLI
reports an error result first (an API error, or the subscription's session
limit). A session limit stops the run before its next case instead, since
every later call would fail the same way.

The SDK has no typed field for the session limit. It reaches us as the CLI's
own text ("You've hit your session limit · resets 6:50pm" on 2026-10-07) or as
an HTTP 429, so this matches on both.
"""

from claude_agent_sdk import ProcessError, ResultError

from recon.contracts import AgentResult, CaseScore

# What a judge call can raise that ends only that call.
JUDGE_ERRORS = (ProcessError,)

_SESSION_LIMIT_MARKERS = ("session limit", "usage limit", "rate limit", "rate_limit")
_TOO_MANY_REQUESTS = 429


def is_session_limit(text: str | None) -> bool:
    """Whether an error message says the subscription's usage limit was hit."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _SESSION_LIMIT_MARKERS)


def error_is_session_limit(exc: ProcessError) -> bool:
    """`is_session_limit` for a raised SDK error, using its HTTP status too."""
    if isinstance(exc, ResultError) and exc.api_error_status == _TOO_MANY_REQUESTS:
        return True
    return is_session_limit(str(exc))


class SessionLimitReached(Exception):
    """Raised by `harness.score_case` when the case ran into the session limit.

    Carries the case's score and result, so the caller keeps the case that
    ran and stops before the next one.
    """

    def __init__(self, score: CaseScore, agent_result: AgentResult) -> None:
        super().__init__(
            f"case {score.case_id} hit the subscription's session limit. "
            "Rerun the remaining cases after it resets."
        )
        self.score = score
        self.agent_result = agent_result
