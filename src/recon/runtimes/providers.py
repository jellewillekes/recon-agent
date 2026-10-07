"""Model providers for routed steps (step 14, #19; docs/adr/0029).

A routed step asks a model for one structured reply, with no tools. The local
provider is Ollama, called over its REST API with httpx, so it needs no new
dependency and no credential: it costs nothing and bills no Anthropic account.
Steps that call tools stay on the Agent SDK, on the Claude subscription.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class ProviderError(RuntimeError):
    """A routed call failed. The message says what to fix."""


@dataclass
class Reply:
    """One structured reply and what it took."""

    structured: dict[str, Any]
    tokens_in: int
    tokens_out: int
    cost_eur: float


class ModelProvider(Protocol):
    """A model that answers one prompt with JSON matching `schema`."""

    async def complete(
        self, system_prompt: str, prompt: str, schema: dict[str, Any]
    ) -> Reply: ...


class OllamaProvider:
    """A local model served by `ollama serve`."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_s: float,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._transport = transport

    async def complete(
        self, system_prompt: str, prompt: str, schema: dict[str, Any]
    ) -> Reply:
        """One `/api/chat` call, constrained to `schema` by Ollama's `format`."""
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0},
        }
        transport = (
            self._transport
            if isinstance(self._transport, httpx.AsyncBaseTransport)
            else None
        )
        async with httpx.AsyncClient(
            base_url=self.base_url, timeout=self.timeout_s, transport=transport
        ) as client:
            try:
                response = await client.post("/api/chat", json=body)
            except httpx.HTTPError as exc:
                raise ProviderError(self._unreachable(exc)) from exc
        if response.status_code != 200:
            raise ProviderError(
                f"Ollama answered {response.status_code} for {self.model}: "
                f"{response.text[:200]}. Check `ollama ps` and the model name."
            )
        payload = response.json()
        try:
            structured = json.loads(payload["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                f"{self.model} didn't reply with JSON. Try a model that follows "
                "JSON schemas more reliably, or route fewer steps."
            ) from exc
        if not isinstance(structured, dict):
            raise ProviderError(f"{self.model} replied with JSON that isn't an object.")
        return Reply(
            structured=structured,
            tokens_in=int(payload.get("prompt_eval_count", 0)),
            tokens_out=int(payload.get("eval_count", 0)),
            cost_eur=0.0,
        )

    def readiness_problem(self) -> str | None:
        """Why a routed eval can't run, checked before any credit is spent."""
        transport = (
            self._transport
            if isinstance(self._transport, httpx.BaseTransport)
            else None
        )
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=5, transport=transport
            ) as client:
                response = client.get("/api/tags")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return self._unreachable(exc)
        names = {model.get("name") for model in response.json().get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            return (
                f"Ollama at {self.base_url} doesn't have {self.model}. "
                f"Run `ollama pull {self.model}` first."
            )
        return None

    def _unreachable(self, exc: Exception) -> str:
        return (
            f"Ollama isn't reachable at {self.base_url} ({type(exc).__name__}). "
            "Start it with `ollama serve`, or run without --routing."
        )


# Steps a local model may take. Only the supervisor's decompose step is a
# plain structured reply; workers, synthesis and the critic stay on Claude.
ROUTABLE_STEPS = ("decompose",)


def local_provider(config: dict[str, Any]) -> OllamaProvider:
    """The local model in `config/models.yaml`'s `routing:` section."""
    routing = config["routing"]
    if routing["provider"] != "ollama":
        raise ValueError(
            f"routing.provider {routing['provider']!r} isn't supported. Only "
            "ollama is (docs/adr/0029)."
        )
    unroutable = set(routing["steps"]) - set(ROUTABLE_STEPS)
    if unroutable:
        raise ValueError(
            f"routing.steps {sorted(unroutable)} can't run on a local model. "
            f"Routable: {list(ROUTABLE_STEPS)}."
        )
    return OllamaProvider(
        base_url=routing["base_url"],
        model=routing["model"],
        timeout_s=float(routing["timeout_s"]),
    )


def routing_problem(runtime: str, mode: str, config: dict[str, Any]) -> str | None:
    """Why `eval --routing on` can't run, checked before any credit is spent."""
    if runtime != "sdk":
        return "--routing on runs on the Agent SDK runtime only (docs/adr/0029)."
    if mode != "multi":
        return (
            "--routing on needs --mode multi. Single mode is one tool-calling "
            "call, so nothing in it is routed."
        )
    return local_provider(config).readiness_problem()
