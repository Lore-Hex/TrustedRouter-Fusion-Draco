"""TrustedRouter SDK transport for the eval harness.

Every gateway chat-completion in this repo goes through the official
``trusted-router-py`` SDK (``pip install trusted-router-py``) rather than a raw
OpenAI-shaped HTTP call — the benchmark dogfoods the same client our users do.
The SDK handles auth, regional failover, and 429/5xx retries; here we adapt its
typed ``ChatCompletion`` back to the OpenAI-shaped dict / response surface the
rest of the harness already speaks, so call sites stay unchanged.
"""
from __future__ import annotations

from typing import Any, Mapping

from trustedrouter import TrustedRouter

DEFAULT_GATEWAY = "https://api.quillrouter.com/v1"


def make_client(*, base_url: str | None = None, api_key: str,
                timeout: float = 600.0, max_retries: int = 3) -> TrustedRouter:
    """One OpenAI-compatible, attested client pointed at the TrustedRouter gateway."""
    return TrustedRouter(
        api_key=api_key,
        base_url=(base_url or DEFAULT_GATEWAY),
        timeout=timeout,
        max_retries=max_retries,
    )


def chat(client: TrustedRouter, body: Mapping[str, Any]) -> dict[str, Any]:
    """Send an OpenAI-shaped chat body via the SDK; return the response as a dict.

    Accepts the exact ``body`` dicts the harness already builds (model, messages,
    tools, response_format, reasoning_effort, max_tokens, temperature, ...).
    """
    model = body.get("model") or "trustedrouter/auto"
    messages = body["messages"]
    params = {k: v for k, v in body.items() if k not in ("model", "messages")}
    # Ask the gateway to report token usage on the collected stream.
    params.setdefault("stream_options", {"include_usage": True})
    return client.chat_completions(model=model, messages=messages, **params).model_dump()


class SdkResponse:
    """Adapts an SDK result dict to the small ``httpx.Response`` surface the
    harness touches (``.json()`` / ``.raise_for_status()`` / ``.status_code``).
    The SDK already raises on HTTP errors, so ``raise_for_status`` is a no-op."""

    status_code = 200

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        # Surface the SDK's response id as a request-id header for parsers that read it.
        self.headers: dict[str, str] = {"x-tr-request-id": str(data.get("id") or "")}

    def json(self) -> dict[str, Any]:
        return self._data

    def raise_for_status(self) -> None:
        return None


def chat_response(client: TrustedRouter, body: Mapping[str, Any]) -> SdkResponse:
    """Like :func:`chat` but wrapped so code expecting an httpx.Response works."""
    return SdkResponse(chat(client, body))
