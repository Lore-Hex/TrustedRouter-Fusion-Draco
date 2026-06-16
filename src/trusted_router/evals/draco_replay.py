from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trusted_router.evals.draco import (
    DRACO_CONFIG,
    DRACO_DATASET,
    DRACO_SPLIT,
    DracoTask,
    DracoTaskFilter,
    filter_draco_tasks,
    parse_draco_task,
)
from trusted_router.evals.fusion_live import (
    DEFAULT_JUDGE_REASONING_EFFORT,
    DEFAULT_TR_CRITERION_JUDGE_MAX_OUTPUT_TOKENS,
    ChatResult,
    CriterionJudgeResult,
    CriterionJudgment,
    TrustedRouterChatClient,
    _chunks,
    _flat_criteria,
    criterion_judge_messages_for_criteria,
    criterion_score,
    parse_criterion_judge_json_for_criteria,
)

DEFAULT_NON_FINANCIAL_TASK_COUNT = 80
DEFAULT_DRACO_REPLICATION_CONFIGS: tuple[str, ...] = (
    "solo_gemini_3_flash",
    "solo_kimi_k2_6",
    "solo_deepseek_v4_pro",
    "solo_gemini_3_1_pro",
    "solo_opus_4_8",
    "fusion_tr_budget",
)

OPENROUTER_DRACO_SCORES: dict[str, float] = {
    "fusion_fable_5_gpt_5_5_opus_4_8": 69.0,
    "fusion_opus_4_8_gpt_5_5_gemini_3_1_pro": 68.3,
    "fusion_opus_4_8_gpt_5_5": 67.6,
    "fusion_opus_4_8_opus_4_8": 65.5,
    "solo_fable_5": 65.3,
    "fusion_tr_budget": 64.7,
    "solo_deepseek_v4_pro": 60.3,
    "solo_gpt_5_5": 60.0,
    "solo_opus_4_8": 58.8,
    "solo_kimi_k2_6": 53.7,
    "solo_gemini_3_1_pro": 45.4,
    "solo_gemini_3_flash": 43.1,
}


@dataclass(frozen=True)
class DracoManifest:
    id: str
    task_filter: DracoTaskFilter
    tasks: tuple[DracoTask, ...]

    def artifact(self) -> dict[str, Any]:
        return {
            "schema": "trustedrouter.draco.manifest.v1",
            "id": self.id,
            "dataset": DRACO_DATASET,
            "config": DRACO_CONFIG,
            "split": DRACO_SPLIT,
            "task_filter": self.task_filter,
            "task_count": len(self.tasks),
            "task_ids": [task.id for task in self.tasks],
            "tasks": [task.cache_dict() for task in self.tasks],
        }


@dataclass(frozen=True)
class ScoreSummary:
    config_id: str
    completed: int
    failed: int
    mean_score: float | None
    openrouter_score: float | None

    @property
    def delta_from_openrouter(self) -> float | None:
        if self.mean_score is None or self.openrouter_score is None:
            return None
        return self.mean_score - self.openrouter_score


def build_draco_manifest(
    tasks: tuple[DracoTask, ...],
    *,
    task_filter: DracoTaskFilter = "non-financial",
    task_count: int = DEFAULT_NON_FINANCIAL_TASK_COUNT,
    manifest_id: str | None = None,
) -> DracoManifest:
    if task_count < 1:
        raise ValueError("task_count must be positive")
    eligible = filter_draco_tasks(tasks, task_filter=task_filter)
    if len(eligible) < task_count:
        raise ValueError(
            f"requested {task_count} tasks but only {len(eligible)} match {task_filter}"
        )
    selected = eligible[:task_count]
    resolved_id = manifest_id or f"draco-{task_filter}-{task_count}"
    return DracoManifest(id=resolved_id, task_filter=task_filter, tasks=selected)


def write_manifest(manifest: DracoManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.artifact(), indent=2, sort_keys=True) + "\n")


def load_manifest(path: Path) -> DracoManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "trustedrouter.draco.manifest.v1":
        raise ValueError("manifest must have schema trustedrouter.draco.manifest.v1")
    manifest_id = _required_str(payload, "id")
    task_filter = _required_str(payload, "task_filter")
    if task_filter not in {"all", "non-financial"}:
        raise ValueError(f"unsupported task_filter in manifest: {task_filter}")
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("manifest must contain tasks")
    tasks = tuple(parse_draco_task(_dict_item(item, "task")) for item in raw_tasks)
    return DracoManifest(
        id=manifest_id,
        task_filter=task_filter,  # type: ignore[arg-type]
        tasks=tasks,
    )


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            yield parsed


def summarize_score_rows(rows: Iterable[dict[str, Any]]) -> tuple[ScoreSummary, ...]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        config_id = row.get("config_id")
        if isinstance(config_id, str):
            grouped.setdefault(config_id, []).append(row)
    summaries: list[ScoreSummary] = []
    for config_id, config_rows in sorted(grouped.items()):
        scores = [
            float(row["score"])
            for row in config_rows
            if row.get("status") != "failed" and isinstance(row.get("score"), int | float)
        ]
        failed = sum(1 for row in config_rows if row.get("status") == "failed")
        mean_score = sum(scores) / len(scores) if scores else None
        summaries.append(
            ScoreSummary(
                config_id=config_id,
                completed=len(scores),
                failed=failed,
                mean_score=mean_score,
                openrouter_score=OPENROUTER_DRACO_SCORES.get(config_id),
            )
        )
    return tuple(summaries)


def markdown_report(summaries: Iterable[ScoreSummary], *, title: str) -> str:
    rows = list(summaries)
    lines = [
        f"# {title}",
        "",
        "| Config | TrustedRouter score | OpenRouter score | Delta | Completed | Failed |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in rows:
        lines.append(
            "| "
            + " | ".join(
                (
                    item.config_id,
                    _score_cell(item.mean_score),
                    _score_cell(item.openrouter_score),
                    _score_cell(item.delta_from_openrouter, signed=True),
                    str(item.completed),
                    str(item.failed),
                )
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "Scores are directly publishable only when the manifest, model tools, judge model, "
        "judge passes, and task set match the documented benchmark run."
    )
    return "\n".join(lines) + "\n"


def final_answer_from_replay(row: dict[str, Any]) -> str:
    final = row.get("final")
    if not isinstance(final, dict):
        raise ValueError("replay row is missing final output")
    content = final.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("replay row final output has no content")
    return content


def task_from_replay(row: dict[str, Any]) -> DracoTask:
    task = row.get("task")
    if not isinstance(task, dict):
        raise ValueError("replay row is missing task")
    return parse_draco_task(task)


def rejudge_replay_row(
    row: dict[str, Any],
    *,
    tr_client: TrustedRouterChatClient,
    judge_model: str,
    judge_passes: int,
    criterion_chunk_size: int,
    judge_max_tokens: int,
    timeout_seconds: float,
    judge_reasoning_effort: str | None = DEFAULT_JUDGE_REASONING_EFFORT,
) -> dict[str, Any]:
    if judge_passes < 1:
        raise ValueError("judge_passes must be positive")
    if criterion_chunk_size < 1:
        raise ValueError("criterion_chunk_size must be positive")
    if judge_max_tokens < 1:
        raise ValueError("judge_max_tokens must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    task = task_from_replay(row)
    answer = final_answer_from_replay(row)
    judges = tuple(
        _judge_replay_answer(
            task,
            answer,
            tr_client=tr_client,
            judge_model=judge_model,
            criterion_chunk_size=criterion_chunk_size,
            judge_max_tokens=judge_max_tokens,
            timeout_seconds=timeout_seconds,
            judge_reasoning_effort=judge_reasoning_effort,
        )
        for _index in range(judge_passes)
    )
    scores = [judge.score for judge in judges if judge.score is not None]
    score = sum(scores) / len(scores) if scores else None
    return {
        "schema": "trustedrouter.fusion_draco.rejudge.v1",
        "source_task_id": row.get("task_id"),
        "task_id": task.id,
        "domain": task.domain,
        "config_id": row.get("config_id"),
        "eval_mode": row.get("eval_mode"),
        "judge_model": judge_model,
        "judge_passes": judge_passes,
        "judge_reasoning_effort": judge_reasoning_effort,
        "scoring_mode": "criteria",
        "score": score,
        "judges": [judge.public_dict(include_content=True) for judge in judges],
    }


def replay_completed_ids(path: Path) -> set[tuple[str, str]]:
    completed: set[tuple[str, str]] = set()
    for row in iter_jsonl(path):
        config_id = row.get("config_id")
        task_id = row.get("task_id") or row.get("source_task_id")
        if isinstance(config_id, str) and isinstance(task_id, str) and row.get("status") != "failed":
            completed.add((config_id, task_id))
    return completed


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def _judge_replay_answer(
    task: DracoTask,
    answer: str,
    *,
    tr_client: TrustedRouterChatClient,
    judge_model: str,
    criterion_chunk_size: int,
    judge_max_tokens: int,
    timeout_seconds: float,
    judge_reasoning_effort: str | None,
) -> CriterionJudgeResult:
    criteria = _flat_criteria(task.rubric)
    judgments_by_id: dict[str, CriterionJudgment] = {}
    raw_results: list[ChatResult] = []
    for chunk in _chunks(criteria, criterion_chunk_size):
        chunk_judgments, chunk_raw_results = _judge_replay_criteria_chunk(
            task,
            answer,
            chunk,
            tr_client=tr_client,
            judge_model=judge_model,
            judge_max_tokens=judge_max_tokens,
            timeout_seconds=timeout_seconds,
            judge_reasoning_effort=judge_reasoning_effort,
        )
        raw_results.extend(chunk_raw_results)
        for judgment in chunk_judgments:
            judgments_by_id[judgment.id] = judgment
    missing = {str(criterion["id"]) for criterion in criteria} - set(judgments_by_id)
    if missing:
        raise ValueError(f"criterion judge response omitted {len(missing)} criteria")
    judgments = tuple(judgments_by_id[str(criterion["id"])] for criterion in criteria)
    return CriterionJudgeResult(
        model=raw_results[0].model,
        score=criterion_score(task.rubric, judgments),
        criteria=judgments,
        raw=raw_results[0],
        raw_chunks=tuple(raw_results),
    )


def _judge_replay_criteria_chunk(
    task: DracoTask,
    answer: str,
    criteria: tuple[dict[str, str | int], ...],
    *,
    tr_client: TrustedRouterChatClient,
    judge_model: str,
    judge_max_tokens: int,
    timeout_seconds: float,
    judge_reasoning_effort: str | None,
) -> tuple[tuple[CriterionJudgment, ...], tuple[ChatResult, ...]]:
    raw = tr_client.complete(
        model=judge_model,
        messages=criterion_judge_messages_for_criteria(task, answer, criteria),
        temperature=0.0,
        max_tokens=max(judge_max_tokens, DEFAULT_TR_CRITERION_JUDGE_MAX_OUTPUT_TOKENS),
        response_format={"type": "json_object"},
        reasoning_effort=judge_reasoning_effort,
        timeout_seconds=timeout_seconds,
    )
    try:
        return parse_criterion_judge_json_for_criteria(criteria, raw.content), (raw,)
    except (json.JSONDecodeError, ValueError):
        if len(criteria) <= 1:
            raise
        midpoint = len(criteria) // 2
        left_judgments, left_raw = _judge_replay_criteria_chunk(
            task,
            answer,
            criteria[:midpoint],
            tr_client=tr_client,
            judge_model=judge_model,
            judge_max_tokens=judge_max_tokens,
            timeout_seconds=timeout_seconds,
            judge_reasoning_effort=judge_reasoning_effort,
        )
        right_judgments, right_raw = _judge_replay_criteria_chunk(
            task,
            answer,
            criteria[midpoint:],
            tr_client=tr_client,
            judge_model=judge_model,
            judge_max_tokens=judge_max_tokens,
            timeout_seconds=timeout_seconds,
            judge_reasoning_effort=judge_reasoning_effort,
        )
        return left_judgments + right_judgments, left_raw + right_raw


def _score_cell(value: float | None, *, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    if signed:
        return f"{value:+0.1f}"
    return f"{value:0.1f}"


def _required_str(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing {field}")
    return value


def _dict_item(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value
