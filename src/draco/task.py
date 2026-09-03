"""Inspect task for running DRACO through AnyEval.

This task deliberately exposes only one model tool: hosted ``web_search`` through
TrustedRouter's Responses API. It never exposes ``web_fetch`` or ``bash`` because
those would let the evaluation process reach arbitrary hosts, unlike AnyEval's
current egress posture. Hosted search keeps outbound retrieval inside the attested
gateway and applies DRACO's blocked-domain and content-level leakage controls.

Consequently this is a search-only variant of the repository's standalone DRACO
harness. Its scores are not comparable to the published table, whose runs also used
``web_fetch`` and ``bash``.
"""

from __future__ import annotations

import asyncio
import json
import os
from importlib.resources import files
from typing import Any, Literal

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import NOANSWER, Score, Target, mean, scorer
from inspect_ai.solver import (
    Generate,
    Solver,
    TaskState,
    generate,
    solver,
    system_message,
    use_tools,
)
from inspect_ai.tool import tool
from inspect_ai.util import store

from trusted_router.evals import tr_sdk
from trusted_router.evals.agentic_tools import (
    make_web_search,
)
from trusted_router.evals.draco import DracoTask
from trusted_router.evals.fusion_live import (
    DEFAULT_TR_API_BASE_URL,
    TrustedRouterChatClient,
    _chunks,
    _flat_criteria,
    criterion_judge_messages_for_criteria,
    criterion_score,
    load_eval_key,
    parse_criterion_judge_json_for_criteria,
)
from trusted_router.evals.tr_search import TrWebSearchClient

ManifestName = Literal[
    "draco-full-100",
    "draco-non-financial-80",
    "draco-financial-20",
]

DEFAULT_MANIFEST: ManifestName = "draco-full-100"
DEFAULT_MAX_TOOL_CALLS = 12
DEFAULT_JUDGE_MODEL = "google/gemini-3.1-pro-preview"
# Reasoning tokens count against this cap. Tests against reasoning judges found that
# even 48k can end with an empty, length-limited completion, so do not inherit the
# standalone harness's historical 3k floor here.
DEFAULT_JUDGE_MAX_OUTPUT_TOKENS = 64_000
DEFAULT_CRITERION_CHUNK_SIZE = 3

GATEWAY_KEY_ENV_VARS = (
    "TR_FUSION_EVAL_API_KEY",
    "TR_API_KEY",
    "TRUSTEDROUTER_API_KEY",
    "TR_SMOKE_API_KEY",
    "TR_API_KEY_FOR_SELF_HEAL",
)
GATEWAY_BASE_URL_ENV_VARS = (
    "TR_FUSION_EVAL_API_BASE_URL",
    "TR_API_BASE_URL",
    "TRUSTEDROUTER_BASE_URL",
)

_MANIFESTS = {
    "draco-full-100": "draco-full-100.manifest.json",
    "draco-non-financial-80": "draco-non-financial-80.manifest.json",
    "draco-financial-20": "draco-financial-20.manifest.json",
}
_SAMPLE_CONTEXT_KEY = "draco:sample-context"

DRACO_INSPECT_SYSTEM_PROMPT = (
    "You are a deep research analyst. Answer the user's research task with a complete, "
    "source-grounded report. Use web_search iteratively to find current, authoritative "
    "primary sources, cross-check important claims, and gather concrete figures, dates, "
    "and names. Cite source URLs inline, show quantitative work explicitly, and state "
    "uncertainty plainly. You have search only: there is no page-fetch or shell tool. "
    "Do not mention benchmark rubrics. When the evidence is sufficient, write only the "
    "final report, without planning or reasoning narration."
)


def load_dataset(manifest: ManifestName | str = DEFAULT_MANIFEST) -> MemoryDataset:
    """Load one of the three manifests embedded in the installed wheel."""
    filename = _MANIFESTS.get(manifest)
    if filename is None:
        choices = ", ".join(sorted(_MANIFESTS))
        raise ValueError(
            f"unknown DRACO manifest {manifest!r}; choose one of: {choices}"
        )
    resource = files("draco").joinpath("data", filename)
    payload = json.loads(resource.read_text(encoding="utf-8"))
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise TypeError(f"packaged DRACO manifest {manifest!r} has no tasks list")
    samples: list[Sample] = []
    for item in raw_tasks:
        if not isinstance(item, dict):
            raise TypeError(
                f"packaged DRACO manifest {manifest!r} contains a non-object task"
            )
        task_id = item.get("id")
        problem = item.get("problem")
        domain = item.get("domain")
        rubric = item.get("rubric")
        if not isinstance(task_id, str) or not isinstance(problem, str):
            raise TypeError(f"packaged DRACO manifest {manifest!r} has an invalid task")
        if not isinstance(domain, str) or not isinstance(rubric, dict):
            raise TypeError(f"packaged DRACO task {task_id!r} has invalid metadata")
        samples.append(
            Sample(
                id=task_id,
                input=problem,
                target="",
                metadata={"domain": domain, "rubric": rubric},
            )
        )
    return MemoryDataset(name=manifest, samples=samples)


def _task_for_search(query: str, rubric: dict[str, Any]) -> DracoTask:
    return DracoTask(
        id="inspect-search", domain="unknown", problem=query, rubric=rubric
    )


def _perform_search(
    query: str,
    rubric: dict[str, Any],
    *,
    gateway_client: Any,
    num_results: int = 5,
) -> str:
    """Run hosted search and apply the standalone harness's result leak filter."""
    hosted_search = TrWebSearchClient(gateway_client)
    search = make_web_search(_task_for_search(query, rubric), hosted_search)
    return search({"query": query, "num_results": num_results})


def _gateway_api_key() -> str:
    for name in GATEWAY_KEY_ENV_VARS:
        if value := load_eval_key(name):
            return value
    names = ", ".join(GATEWAY_KEY_ENV_VARS)
    raise RuntimeError(
        "TrustedRouter gateway API key is required for DRACO web_search; "
        f"set one of: {names}"
    )


def _gateway_base_url() -> str:
    for name in GATEWAY_BASE_URL_ENV_VARS:
        if value := os.environ.get(name):
            return value
    return DEFAULT_TR_API_BASE_URL


def _gateway_search(query: str, rubric: dict[str, Any], *, num_results: int = 5) -> str:
    key = _gateway_api_key()
    client = tr_sdk.make_client(base_url=_gateway_base_url(), api_key=key)
    try:
        return _perform_search(
            query,
            rubric,
            gateway_client=client,
            num_results=num_results,
        )
    finally:
        client.close()


@solver
def _prepare_sample_context() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        rubric = state.metadata.get("rubric")
        if not isinstance(rubric, dict):
            raise TypeError("DRACO sample metadata is missing its scorer-only rubric")
        store().set(_SAMPLE_CONTEXT_KEY, {"rubric": rubric, "tool_calls": 0})
        return state

    return solve


@tool
def web_search(max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS):
    async def execute(query: str, num_results: int = 5) -> str:
        """Search current web sources through TrustedRouter's hosted search.

        Args:
            query: A focused web search query.
            num_results: Number of cited sources to return, from 1 to 10.
        """
        context = store().get(_SAMPLE_CONTEXT_KEY)
        if not isinstance(context, dict) or not isinstance(context.get("rubric"), dict):
            raise TypeError("DRACO web_search has no initialized sample context")
        calls = int(context.get("tool_calls") or 0)
        if calls >= max_tool_calls:
            return (
                f"Search budget exhausted after {max_tool_calls} calls. "
                "Write the final report using the evidence already gathered."
            )
        context["tool_calls"] = calls + 1
        bounded_results = num_results if isinstance(num_results, int) else 5
        bounded_results = max(1, min(10, bounded_results))
        return await asyncio.to_thread(
            _gateway_search,
            query,
            context["rubric"],
            num_results=bounded_results,
        )

    return execute


def _make_judge_client() -> TrustedRouterChatClient:
    return TrustedRouterChatClient(
        _gateway_api_key(),
        base_url=_gateway_base_url(),
        timeout_seconds=600.0,
    )


def _judge_chunk(
    *,
    client: Any,
    task_item: DracoTask,
    answer: str,
    criteria: tuple[dict[str, str | int], ...],
    judge_model: str,
    judge_max_tokens: int,
) -> tuple[Any, ...]:
    raw = client.complete(
        model=judge_model,
        messages=criterion_judge_messages_for_criteria(task_item, answer, criteria),
        temperature=0.0,
        max_tokens=max(judge_max_tokens, DEFAULT_JUDGE_MAX_OUTPUT_TOKENS),
        response_format={"type": "json_object"},
        reasoning_effort="high",
        timeout_seconds=600.0,
    )
    if not str(raw.content or "").strip():
        raise ValueError("criterion judge returned an empty completion")
    try:
        return parse_criterion_judge_json_for_criteria(criteria, raw.content)
    except (json.JSONDecodeError, ValueError):
        if len(criteria) <= 1:
            raise
        midpoint = len(criteria) // 2
        return _judge_chunk(
            client=client,
            task_item=task_item,
            answer=answer,
            criteria=criteria[:midpoint],
            judge_model=judge_model,
            judge_max_tokens=judge_max_tokens,
        ) + _judge_chunk(
            client=client,
            task_item=task_item,
            answer=answer,
            criteria=criteria[midpoint:],
            judge_model=judge_model,
            judge_max_tokens=judge_max_tokens,
        )


def _judge_answer(
    *,
    client: Any,
    problem: str,
    domain: str,
    rubric: dict[str, Any],
    answer: str,
    judge_model: str,
    judge_max_tokens: int,
) -> tuple[float, tuple[Any, ...]]:
    task_item = DracoTask(
        id="inspect-score",
        domain=domain,
        problem=problem,
        rubric=rubric,
    )
    criteria = _flat_criteria(rubric)
    judgments = tuple(
        judgment
        for chunk in _chunks(criteria, DEFAULT_CRITERION_CHUNK_SIZE)
        for judgment in _judge_chunk(
            client=client,
            task_item=task_item,
            answer=answer,
            criteria=chunk,
            judge_model=judge_model,
            judge_max_tokens=judge_max_tokens,
        )
    )
    if len(judgments) != len(criteria):
        raise ValueError("criterion judge did not return every rubric verdict")
    return criterion_score(rubric, judgments) / 100.0, judgments


@scorer(metrics=[mean()])
def draco_scorer(
    judge_model: str = DEFAULT_JUDGE_MODEL,
    judge_max_tokens: int = DEFAULT_JUDGE_MAX_OUTPUT_TOKENS,
):
    """Judge every rubric criterion and return its weighted fraction in [0, 1]."""

    async def score(state: TaskState, target: Target) -> Score:
        del target
        rubric = state.metadata.get("rubric")
        domain = state.metadata.get("domain")
        if not isinstance(rubric, dict) or not isinstance(domain, str):
            return Score(
                value=NOANSWER,
                reason="scoring_failed",
                explanation="DRACO sample metadata did not contain a usable rubric.",
            )
        answer = state.output.completion
        client: Any | None = None
        try:
            client = _make_judge_client()
            value, judgments = await asyncio.to_thread(
                _judge_answer,
                client=client,
                problem=str(state.input),
                domain=domain,
                rubric=rubric,
                answer=answer,
                judge_model=judge_model,
                judge_max_tokens=judge_max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - grader failure is an unscored sample
            return Score(
                value=NOANSWER,
                reason="grader_failed",
                explanation=f"No usable criterion verdict: {str(exc)[:240]}",
            )
        finally:
            if client is not None:
                client.close()
        met = sum(1 for judgment in judgments if judgment.met)
        return Score(
            value=value,
            explanation=(
                f"{met}/{len(judgments)} criteria met; weighted score {value:.3f}."
            ),
            metadata={
                "judge_model": judge_model,
                "criteria": [
                    judgment.public_dict(include_content=True) for judgment in judgments
                ],
            },
        )

    return score


@task
def draco(
    manifest: ManifestName | str = DEFAULT_MANIFEST,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    judge_max_tokens: int = DEFAULT_JUDGE_MAX_OUTPUT_TOKENS,
) -> Task:
    """Build the AnyEval DRACO task (full 100 by default)."""
    if max_tool_calls < 1:
        raise ValueError("max_tool_calls must be positive")
    if judge_max_tokens < DEFAULT_JUDGE_MAX_OUTPUT_TOKENS:
        raise ValueError(
            f"judge_max_tokens must be at least {DEFAULT_JUDGE_MAX_OUTPUT_TOKENS}"
        )
    return Task(
        dataset=load_dataset(manifest),
        solver=[
            _prepare_sample_context(),
            system_message(DRACO_INSPECT_SYSTEM_PROMPT),
            use_tools([web_search(max_tool_calls=max_tool_calls)]),
            generate(),
        ],
        scorer=draco_scorer(
            judge_model=judge_model,
            judge_max_tokens=judge_max_tokens,
        ),
        # The tool itself enforces the exact external-call cap. This second bound
        # prevents a model from looping forever on the budget-exhausted response.
        message_limit=(2 * max_tool_calls) + 8,
        metadata={
            "variant": "search-only",
            "score_comparability": "not comparable to published fetch-and-bash runs",
        },
    )


__all__ = [
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_MAX_TOOL_CALLS",
    "draco",
    "draco_scorer",
    "load_dataset",
    "web_search",
]
