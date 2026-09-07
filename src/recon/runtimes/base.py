"""The one interface every agent runtime implements.

`docs/contracts.md` section 4: "Every runtime produces exactly this object.
Runtimes are interchangeable as long as this contract holds." The harness
(step 5) depends only on this protocol, never on a specific runtime module.
"""

from typing import Protocol

from recon.contracts import AgentResult, Case


class Runtime(Protocol):
    """Something that can answer a `Case` and report an `AgentResult`."""

    def run(self, case: Case) -> AgentResult:
        """Answer `case`. Never raises — a failed run populates `error` instead."""
        ...

    async def run_async(self, case: Case) -> AgentResult:
        """Same contract as `run`, but a real coroutine: cancelling the await
        actually stops the underlying work, not just the wrapper around it.
        """
        ...
