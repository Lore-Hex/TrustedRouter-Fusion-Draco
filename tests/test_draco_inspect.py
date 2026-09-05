from __future__ import annotations

import asyncio
import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from inspect_ai._util.registry import registry_info
from inspect_ai.scorer import NOANSWER, Target
from inspect_ai.util import store

import draco.task as draco_task
from draco import draco
from trusted_router.evals.agentic_tools import DRACO_BLOCKED_DOMAINS

REPO_MANIFEST = Path(__file__).parents[1] / "data" / "draco-full-100.manifest.json"


def _criteria_rubric(count: int = 4) -> dict[str, Any]:
    return {
        "id": "unit-rubric",
        "sections": [
            {
                "criteria": [
                    {
                        "id": f"criterion-{index:02d}-long-enough",
                        "requirement": (
                            f"Criterion {index} requires this deliberately long unique phrase "
                            "whose first ten words can identify leaked answer key content"
                        ),
                        "weight": 1,
                    }
                    for index in range(count)
                ]
            }
        ],
    }


class StubGateway:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(
        self, method: str, path: str, *, json: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((method, path, json))
        return self.body

    def close(self) -> None:
        return None


class StubJudge:
    """A stand-in for the Inspect model the scorer gets from get_model()."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = iter(replies)
        self.calls: list[dict[str, Any]] = []

    async def generate(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"input": input, "config": config, **kwargs})
        return SimpleNamespace(completion=next(self.replies))


def _score_with(
    monkeypatch: pytest.MonkeyPatch, judge: StubJudge, rubric: dict[str, Any]
):
    judge.requested = []

    def fake_get_model(model_id: str) -> StubJudge:
        judge.requested.append(model_id)
        return judge

    monkeypatch.setattr(draco_task, "get_model", fake_get_model)
    state = SimpleNamespace(
        input="A test problem",
        metadata={"rubric": rubric, "domain": "Academic"},
        output=SimpleNamespace(completion="A candidate report"),
    )
    return asyncio.run(draco_task.draco_scorer()(state, Target("")))


def test_packaged_full_dataset_has_exact_ids_and_never_exposes_rubric() -> None:
    manifest = json.loads(REPO_MANIFEST.read_text(encoding="utf-8"))
    dataset = draco_task.load_dataset("draco-full-100")

    assert len(dataset) == 100, "packaged full manifest must load exactly 100 samples"
    assert [sample.id for sample in dataset] == manifest["task_ids"], (
        "sample ids must match the full manifest ids verbatim"
    )
    for sample, raw_task in zip(dataset, manifest["tasks"], strict=True):
        for section in raw_task["rubric"]["sections"]:
            for criterion in section["criteria"]:
                fragment = criterion["requirement"]
                assert fragment not in sample.input, (
                    f"rubric leaked into sample input for {sample.id}"
                )
                targets = (
                    sample.target
                    if isinstance(sample.target, list)
                    else [sample.target]
                )
                assert all(fragment not in target for target in targets), (
                    f"rubric leaked into sample target for {sample.id}"
                )
        assert sample.metadata["rubric"] == raw_task["rubric"]


@pytest.mark.parametrize(
    ("manifest_name", "expected"),
    [("draco-non-financial-80", 80), ("draco-financial-20", 20)],
)
def test_packaged_splits_load_by_manifest_name(
    manifest_name: str, expected: int
) -> None:
    assert len(draco_task.load_dataset(manifest_name)) == expected


def test_web_search_uses_hosted_gateway_contract_and_returns_summary_and_sources() -> (
    None
):
    rubric = _criteria_rubric()
    gateway = StubGateway(
        {
            "id": "resp-test",
            "status": "completed",
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "query": "issued query",
                        "sources": [
                            {
                                "title": "Primary source",
                                "url": "https://example.com/source",
                            }
                        ],
                    },
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Grounded summary."}],
                },
            ],
        }
    )

    output = draco_task._perform_search("test query", rubric, gateway_client=gateway)

    assert "Grounded summary." in output
    assert "Primary source" in output
    assert "https://example.com/source" in output
    _method, _path, payload = gateway.calls[0]
    search_tool = payload["tools"][0]
    assert set(DRACO_BLOCKED_DOMAINS) <= set(
        search_tool.get("filters", {}).get("blocked_domains", [])
    ), "hosted search request omitted DRACO blocked_domains"
    assert payload["tool_choice"] == "required", (
        "hosted search must require the gateway web_search tool"
    )
    assert payload["include"] == ["web_search_call.action.sources"], (
        "hosted search must request cited source metadata"
    )


def test_web_search_filters_rubric_fragment_before_model_sees_it() -> None:
    rubric = _criteria_rubric()
    requirement = rubric["sections"][0]["criteria"][0]["requirement"]
    leaked_fragment = " ".join(requirement.split()[:10])
    gateway = StubGateway(
        {
            "id": "resp-leak",
            "status": "completed",
            "output": [
                {
                    "type": "web_search_call",
                    "action": {
                        "sources": [
                            {"title": "Safe source", "url": "https://example.org/safe"}
                        ]
                    },
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": leaked_fragment}],
                },
            ],
        }
    )

    output = draco_task._perform_search("test query", rubric, gateway_client=gateway)

    assert leaked_fragment not in output, (
        "rubric criterion fragment reached the model through web_search"
    )
    assert "Safe source" in output


def test_web_search_without_gateway_key_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(draco_task, "load_eval_key", lambda _name: None)
    for name in draco_task.GATEWAY_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    store().set(
        draco_task._SAMPLE_CONTEXT_KEY,
        {"rubric": _criteria_rubric(), "tool_calls": 0},
    )

    with pytest.raises(RuntimeError, match="TrustedRouter gateway API key is required"):
        asyncio.run(draco_task.web_search()("test query"))


def test_web_search_enforces_exact_tool_call_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_search(
        query: str, _rubric: dict[str, Any], *, num_results: int = 5
    ) -> str:
        calls.append(query)
        return f"result {num_results}"

    monkeypatch.setattr(draco_task, "_gateway_search", fake_search)
    store().set(
        draco_task._SAMPLE_CONTEXT_KEY,
        {"rubric": _criteria_rubric(), "tool_calls": 0},
    )
    search = draco_task.web_search(max_tool_calls=2)

    async def run_searches() -> list[str]:
        return [await search(f"query {index}") for index in range(3)]

    results = asyncio.run(run_searches())

    assert calls == ["query 0", "query 1"], (
        "web_search exceeded its exact tool-call cap"
    )
    assert "Search budget exhausted after 2 calls" in results[-1]


def test_scorer_three_of_four_is_point_seven_five_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rubric = _criteria_rubric()
    first_three = [item["id"] for item in rubric["sections"][0]["criteria"][:3]]
    last = rubric["sections"][0]["criteria"][3]["id"]
    judge = StubJudge(
        [
            json.dumps(
                {
                    "criteria": [
                        {"id": first_three[0], "met": True},
                        {"id": first_three[1], "met": True},
                        {"id": first_three[2], "met": False},
                    ]
                }
            ),
            json.dumps({"criteria": [{"id": last, "met": True}]}),
        ]
    )

    score = _score_with(monkeypatch, judge, rubric)

    assert score.value == 0.75, "three of four equal-weight criteria must score 0.75"
    assert [item["met"] for item in score.metadata["criteria"]] == [
        True,
        True,
        False,
        True,
    ]
    # The judge is called through Inspect's model API (get_model), so the settings
    # travel in a GenerateConfig — that is what lets AnyEval price, receipt and
    # attribute the call as the grader instead of seeing an unpriced envelope.
    # No reasoning_effort on the judge: the gateway maps it to a Gemini "thinking" field
    # Google rejects for this model, and AnyEval disables provider fallbacks.
    assert all(call["config"].reasoning_effort is None for call in judge.calls)
    assert all(call["config"].max_tokens >= 64_000 for call in judge.calls), (
        "reasoning judge output budget must be at least 64k tokens"
    )
    assert all(call["config"].temperature == 0.0 for call in judge.calls)
    # The id handed to get_model is the provider-addressed one the task declares: a bare
    # google/... id would resolve to Inspect's own Google provider and leave the gateway.
    assert judge.requested == [draco_task.DEFAULT_JUDGE_MODEL], judge.requested
    assert judge.requested[0].startswith("trustedrouter/")


def test_empty_judge_reply_is_unscored_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score = _score_with(monkeypatch, StubJudge(["", "", ""]), _criteria_rubric(1))

    assert score.value == NOANSWER, (
        "empty judge reply must be unscored/NOANSWER, not zero"
    )


def test_inspect_registry_resolves_draco_reference_and_declares_components() -> None:
    assert draco.__name__ == "draco", "Inspect task must be registered as draco/draco"
    entry_points = importlib.metadata.entry_points(group="inspect_ai")
    assert any(ep.name == "draco" and ep.value == "draco" for ep in entry_points), (
        "Inspect entry point must expose the draco/draco task namespace"
    )
    built = draco()
    assert built.scorer is not None, (
        "DRACO Inspect task must declare its criterion scorer"
    )
    assert built.solver is not None, (
        "DRACO Inspect task must declare its agentic solver"
    )
    assert registry_info(built.scorer[0]).name.endswith("draco_scorer"), (
        "DRACO Inspect task must use draco_scorer"
    )
    solver_spec = json.dumps(built.solver.__registry_params__)
    assert '"name": "web_search"' in solver_spec, (
        "DRACO Inspect solver must declare web_search"
    )
    assert (
        '"name": "web_fetch"' not in solver_spec and '"name": "bash"' not in solver_spec
    ), "DRACO Inspect solver must expose only web_search"
