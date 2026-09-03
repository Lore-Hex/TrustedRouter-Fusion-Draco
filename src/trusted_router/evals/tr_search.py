"""Web search through TrustedRouter's own hosted tool, instead of calling Exa direct.

WHY THIS EXISTS. ``agentic_tools`` was written when "our gateway has no server-side
tool execution", so the harness replicated OpenRouter's ``openrouter:web_search`` by
calling api.exa.ai from the client with an ``EXA_API_KEY``. The gateway has hosted
search now: a Responses-API ``web_search`` tool that asks the model for bounded queries,
calls Exa *from inside the attested enclave*, and returns cited sources. Routing search
through it is the point of this module — the benchmark then dogfoods the same search
path a TrustedRouter customer gets, and the harness needs no Exa credential at all.

WHAT THE GATEWAY RETURNS, AND WHAT IT DOES NOT. A hosted search call yields, per
search, the query the model chose and its sources as ``{title, type, url}``. It does
NOT hand back page text: the retrieved content is consumed by the model inside the
gateway, and what comes out is the model's cited prose. That is a real difference from
the Exa backend, which returned highlights and full text per result, so this client
reports the prose as an explicit, clearly-labelled summary result rather than pretending
some URL contributed text it never returned. Depth is still available to the outer
model: ``web_fetch`` retrieves any of the returned URLs in full, client-side, exactly
as before.

The class is shaped as a drop-in for ``ExaSearchClient`` — same ``search_with_contents``
signature, same ``ExaSearchBundle`` out — so every DRACO leakage control keeps working
untouched: blocked domains go to the gateway as ``filters.blocked_domains``, and the
summary and every source still pass through the harness's content-level leak scan
because they arrive as ordinary ``ExaResult`` rows.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from trusted_router.evals.exa import ExaResult, ExaSearchBundle

# The gateway allows at most three hosted search calls per response; asking for a
# tighter context keeps a single search cheap and fast, which is what the harness
# wants when the model is issuing many small searches of its own accord.
DEFAULT_SEARCH_MODEL = "deepseek/deepseek-v4-flash-0731"
DEFAULT_CONTEXT_SIZE = "medium"
SUMMARY_TITLE = "TrustedRouter hosted search summary (Exa, in-gateway)"


class TrWebSearchError(RuntimeError):
    """A hosted search that produced neither sources nor prose."""


def _searcher_instructions(query: str) -> str:
    return (
        "Search the web for the query below and report what you found. Quote concrete "
        "figures, dates and names, and attribute each to the source it came from. Do "
        "not answer from memory: every claim must come from a search result.\n\n"
        f"Query: {query}"
    )


class TrWebSearchClient:
    """``ExaSearchClient``-shaped search backed by the gateway's hosted web_search."""

    def __init__(
        self,
        client: Any,
        *,
        model: str = DEFAULT_SEARCH_MODEL,
        context_size: str = DEFAULT_CONTEXT_SIZE,
        max_output_tokens: int = 1200,
    ) -> None:
        self._client = client
        self._model = model
        self._context_size = context_size
        self._max_output_tokens = max_output_tokens
        self.last_cost_microdollars: int = 0

    def close(self) -> None:  # the harness closes its search client; ours is borrowed
        return None

    def search_with_contents(
        self,
        query: str,
        *,
        exclude_domains: Sequence[str] = (),
        num_results: int = 5,
        **_ignored: Any,
    ) -> ExaSearchBundle:
        tool: dict[str, Any] = {
            "type": "web_search",
            "search_context_size": self._context_size,
        }
        if exclude_domains:
            # One list only — the gateway rejects allowed_domains and blocked_domains
            # together, and DRACO's leakage control is a block list.
            tool["filters"] = {"blocked_domains": list(dict.fromkeys(exclude_domains))}

        payload = {
            "model": self._model,
            "input": _searcher_instructions(query),
            "tools": [tool],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "store": False,
            "max_output_tokens": self._max_output_tokens,
        }
        body = self._client.request("POST", "/responses", json=payload)
        return self._bundle_from_response(query, body, num_results=num_results)

    def _bundle_from_response(
        self, query: str, body: Mapping[str, Any], *, num_results: int
    ) -> ExaSearchBundle:
        if not isinstance(body, Mapping):  # pragma: no cover - transport contract
            raise TrWebSearchError(f"unexpected hosted-search response: {type(body)!r}")
        if body.get("error"):
            raise TrWebSearchError(str(body["error"])[:300])

        summary_parts: list[str] = []
        sources: list[tuple[str, str]] = []
        issued_queries: list[str] = []
        for item in body.get("output") or []:
            if not isinstance(item, Mapping):
                continue
            kind = str(item.get("type") or "")
            if kind.endswith("web_search_call"):
                action = item.get("action") or {}
                if isinstance(action, Mapping):
                    if issued := str(action.get("query") or "").strip():
                        issued_queries.append(issued)
                    for source in action.get("sources") or []:
                        if not isinstance(source, Mapping):
                            continue
                        url = str(source.get("url") or "").strip()
                        if url:
                            sources.append((str(source.get("title") or url), url))
            elif kind == "message":
                for part in item.get("content") or []:
                    if isinstance(part, Mapping) and (text := part.get("text")):
                        summary_parts.append(str(text))

        usage = body.get("usage") or {}
        if isinstance(usage, Mapping):
            self.last_cost_microdollars = int(usage.get("cost_microdollars") or 0)

        summary = "\n".join(part.strip() for part in summary_parts if part.strip()).strip()
        if not summary and not sources:
            raise TrWebSearchError(
                "hosted search returned no sources and no prose "
                f"(status={body.get('status')!r})"
            )

        results: list[ExaResult] = []
        if summary:
            # Labelled as the gateway's own prose, with the queries it actually issued,
            # so a reader of the replay can never mistake it for a page extract.
            issued = "; ".join(dict.fromkeys(issued_queries))
            header = f"Queries issued: {issued}\n\n" if issued else ""
            results.append(
                ExaResult(
                    title=SUMMARY_TITLE,
                    url="",
                    published_date=None,
                    author=None,
                    highlights=(),
                    text=f"{header}{summary}",
                )
            )
        seen: set[str] = set()
        for title, url in sources:
            if url in seen:
                continue
            seen.add(url)
            results.append(
                ExaResult(
                    title=title,
                    url=url,
                    published_date=None,
                    author=None,
                    highlights=(),
                    text=None,
                )
            )
            if len(results) >= num_results + 1:
                break

        return ExaSearchBundle(
            query=query,
            request_id=str(body.get("id") or "") or None,
            resolved_search_type="trustedrouter_hosted_web_search",
            cost_dollars=(self.last_cost_microdollars / 1_000_000) or None,
            results=tuple(results),
        )
