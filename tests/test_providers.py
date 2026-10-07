"""The local model provider for routed steps (step 14, #19). No network: the
Ollama server is an httpx mock transport."""

import json
from typing import Any

import httpx
import pytest

from recon.runtimes import providers

pytestmark = [pytest.mark.unit, pytest.mark.anyio]

SCHEMA = {
    "type": "object",
    "properties": {"subtasks": {"type": "array"}},
    "required": ["subtasks"],
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _provider(handler: Any) -> providers.OllamaProvider:
    return providers.OllamaProvider(
        base_url="http://ollama.test",
        model="local-model",
        timeout_s=5,
        transport=httpx.MockTransport(handler),
    )


async def test_a_reply_is_parsed_with_its_token_counts_and_no_cost() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["headers"] = {k.lower() for k in request.headers}
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps({"subtasks": [{"worker": "w"}]})},
                "prompt_eval_count": 120,
                "eval_count": 30,
            },
        )

    reply = await _provider(handler).complete("system", "question?", SCHEMA)

    assert reply.structured == {"subtasks": [{"worker": "w"}]}
    assert (reply.tokens_in, reply.tokens_out, reply.cost_eur) == (120, 30, 0.0)
    assert seen["path"] == "/api/chat"
    assert seen["body"]["model"] == "local-model"
    assert seen["body"]["format"] == SCHEMA
    assert seen["body"]["stream"] is False
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]
    # A local call: no Anthropic key or any credential is sent.
    assert not seen["headers"] & {"x-api-key", "authorization"}


async def test_a_reply_that_is_not_json_fails_the_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not json"}})

    with pytest.raises(providers.ProviderError, match="JSON"):
        await _provider(handler).complete("system", "q", SCHEMA)


async def test_an_unreachable_server_fails_the_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(providers.ProviderError, match="ollama serve"):
        await _provider(handler).complete("system", "q", SCHEMA)


async def test_a_server_error_fails_the_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "model crashed"})

    with pytest.raises(providers.ProviderError, match="500"):
        await _provider(handler).complete("system", "q", SCHEMA)


@pytest.mark.parametrize(
    ("models", "problem"),
    [(["local-model:latest", "other"], None), (["other"], "ollama pull local-model")],
)
async def test_the_readiness_check_finds_the_pulled_model(
    models: list[str], problem: str | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": n} for n in models]})

    result = _provider(handler).readiness_problem()
    if problem is None:
        assert result is None
    else:
        assert result is not None and problem in result


async def test_the_readiness_check_reports_an_unreachable_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert "ollama serve" in (_provider(handler).readiness_problem() or "")


def test_the_repo_routes_decompose_to_a_local_ollama_model() -> None:
    from pathlib import Path

    import yaml

    config = yaml.safe_load(Path("config/models.yaml").read_text())
    provider = providers.local_provider(config)
    assert isinstance(provider, providers.OllamaProvider)
    assert provider.base_url == "http://localhost:11434"
    assert provider.model == "qwen2.5:7b-instruct"
    assert config["routing"]["steps"] == ["decompose"]


@pytest.mark.parametrize(
    "routing",
    [
        {
            "provider": "openai",
            "base_url": "x",
            "model": "m",
            "timeout_s": 5,
            "steps": ["decompose"],
        },
        {
            "provider": "ollama",
            "base_url": "x",
            "model": "m",
            "timeout_s": 5,
            "steps": ["critic"],
        },
    ],
    ids=["unknown-provider", "unroutable-step"],
)
def test_routing_config_refuses_what_isnt_built(routing: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        providers.local_provider({"routing": routing})
