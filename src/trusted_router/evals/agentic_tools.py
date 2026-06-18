"""Client-side agentic tool loop for DRACO live-tool parity with OpenRouter.

OpenRouter's "Fusion beats frontier" DRACO run gave every model (panel + solo)
the same three server tools: ``openrouter:web_search`` (Exa), ``openrouter:web_fetch``
(Exa), and ``openrouter:bash``. Our gateway has no server-side tool execution, so
we replicate the harness *client-side*: the model issues OpenAI-style function
tool-calls through the gateway-as-plain-proxy, and this module executes them and
feeds results back until the model returns a final answer.

Tools:
  * ``web_search``  -> Exa search (excludeDomains enforced for leakage control)
  * ``web_fetch``   -> URL fetch + extraction (blocked-domain + content leak guard)
  * ``bash``        -> sandboxed command in a Docker container (``--network none``)

Leakage controls mirror OpenRouter's: the DRACO rubric/dataset hosts are blocked
on search and fetch, and every tool result is scanned by the same content-level
leak detector the frozen pipeline uses (rubric criterion ids / requirement
fragments / forbidden terms), so the model can neither discover nor retrieve the
benchmark answer key.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from trusted_router.evals import tr_sdk
from trusted_router.evals.draco import DRACO_EXCLUDED_SEARCH_DOMAINS, DracoTask
from trusted_router.evals.exa import (
    ExaResult,
    ExaSearchBundle,
    ExaSearchClient,
    _is_fetchable_public_url,
    fetch_result_text,
)
from trusted_router.evals.fusion_live import _draco_search_result_leak_reason

# markitdown gives high-fidelity, table-preserving markdown for filings / PDFs /
# spreadsheets — important for the DRACO finance slice. Optional: fall back to the
# plain-text fetcher if it isn't installed.
try:
    from markitdown import MarkItDown

    _MARKITDOWN: "MarkItDown | None" = MarkItDown()
except Exception:  # noqa: BLE001
    _MARKITDOWN = None

# SEC and many sites 403 the default UA; declare a research contact (SEC requires one).
_FETCH_HEADERS = {
    "User-Agent": "TrustedRouter-Research research@quillrouter.com",
    "Accept": "*/*",
}
_MAX_FETCH_BYTES = 8_000_000


def _wants_markitdown(url: str, content_type: str) -> str | None:
    """Return a file extension for markitdown when the doc benefits from it
    (PDF, spreadsheets, SEC/EDGAR filings — table-heavy), else None."""
    low = url.lower()
    ct = (content_type or "").lower()
    if "pdf" in ct or low.endswith(".pdf"):
        return ".pdf"
    if "spreadsheet" in ct or "excel" in ct or low.endswith((".xlsx", ".xls")):
        return ".xlsx"
    if low.endswith(".csv") or "text/csv" in ct:
        return ".csv"
    if "sec.gov" in low or "/edgar" in low or "edgar" in low:
        return ".html"
    return None

# Hosts that publish the DRACO paper / rubric / leaderboard. Blocked on both
# search and fetch (OpenRouter blocked "the locations where the results are
# hosted"). We intentionally do NOT blanket-block arxiv.org: many DRACO tasks
# need primary papers, and the content-level leak filter catches the rubric even
# if a page slips through.
DRACO_BLOCKED_DOMAINS: tuple[str, ...] = DRACO_EXCLUDED_SEARCH_DOMAINS + (
    "research.perplexity.ai",
    "r2cdn.perplexity.ai",
)

DEFAULT_BASH_IMAGE = "python:3.12-slim"
# Research budget. Deep-research tasks want many searches, so this is generous;
# when it is reached we do a dedicated synthesis turn rather than abruptly cutting
# the loop (which made models leak tool-call markup or refuse to write).
DEFAULT_MAX_TOOL_CALLS = 16
DEFAULT_SYNTHESIS_MAX_TOKENS = 12_000
# Local tool implementation (Exa search API + local fetch + Docker bash). web_search
# returns lean highlight snippets (discovery); web_fetch returns generous full-page
# text (the model fetches the URLs it wants to read in depth).
DEFAULT_SEARCH_RESULTS = 5
DEFAULT_SEARCH_RESULT_CHARS = 1_800
DEFAULT_FETCH_CHARS = 25_000
MAX_TOOL_RESULT_CHARS = 40_000
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

SYNTHESIS_INSTRUCTION = (
    "You have reached your research budget. Using ONLY the evidence already gathered "
    "in this conversation, write your COMPLETE and FINAL research report now. It must "
    "directly and fully answer the original task, be well structured, cite source URLs "
    "inline, and show any quantitative work. Do NOT call or attempt to call any tools. "
    "Do NOT include planning, reasoning narration, scratchpad text, or any tool-call "
    "syntax — output only the final report."
)

# Markers for tool-call syntax some models leak into visible content (DeepSeek's
# native DSML format; Kimi/Claude-style <invoke>/<function_calls>). We truncate the
# answer at the first such marker as a safety net.
_TOOL_MARKUP_MARKERS = (
    "<｜｜DSML｜｜",
    "<｜tool",
    "<function_calls>",
    "<invoke name=",
    "<invoke>",
    "<tool_call>",
    "<tool_calls>",
)


def strip_tool_markup(content: str) -> str:
    """Truncate visible content at the first leaked tool-call marker."""
    cut = len(content)
    for marker in _TOOL_MARKUP_MARKERS:
        idx = content.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return content[:cut].rstrip()

DRACO_AGENTIC_SYSTEM_PROMPT = (
    "You are a deep research analyst. Answer the user's research task with a "
    "complete, source-grounded report. You have three tools: web_search (find "
    "sources), web_fetch (read a specific URL in full), and bash (run shell / "
    "python for any calculation or data manipulation). Search iteratively: start "
    "broad, then fetch the most authoritative primary sources and verify key "
    "figures with bash. Cite source URLs inline. Show quantitative work explicitly "
    "and state uncertainty plainly. Do not mention benchmark rubrics. When you have "
    "enough evidence, write the final report as plain text with no further tool calls. "
    "Your final report must contain only the report itself — no planning, reasoning "
    "narration, or scratchpad text."
)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current, authoritative sources. Returns titles, URLs, and extracts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "num_results": {"type": "integer", "description": "How many results (max 10)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and extract the readable text of a specific URL (HTML or PDF).",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "The URL to fetch."}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command in an isolated sandbox (python3 available, no network). Use for calculations and data manipulation.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to run."}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sec_facts",
            "description": (
                "Fetch EXACT, as-filed financial figures for a U.S. SEC filer straight from "
                "EDGAR XBRL (operating cash flow, net income, cash, capex, share repurchases, "
                "dividends, debt issuance/repayment, share-based comp, stockholders' equity, "
                "revenue). Returns dollar-exact values with their unit, period end date, fiscal "
                "year/quarter, and form. Use this INSTEAD of web_fetch for any reported number "
                "from a named U.S. public company — the values are scale- and period-unambiguous, "
                "so it avoids thousands-vs-millions and prior-year-column mistakes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol or 10-digit CIK of the U.S. filer."},
                    "metric": {
                        "type": "string",
                        "description": (
                            "Optional. One of: operating_cash_flow, net_income, cash, capex, "
                            "repurchases, dividends, debt_issued, debt_repaid, share_based_comp, "
                            "stockholders_equity, revenue. Omit to get the full capital-allocation set."
                        ),
                    },
                    "fiscal_year": {"type": "integer", "description": "Optional fiscal year filter (e.g. 2024)."},
                },
                "required": ["ticker"],
            },
        },
    },
]


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any]
    result_chars: int
    error: str | None = None


@dataclass
class AgenticResult:
    content: str
    finish_reason: str | None
    tool_calls_made: int
    tool_records: list[ToolCallRecord]
    input_tokens: int | None
    output_tokens: int | None
    elapsed_ms: int
    steps: int
    truncated_loop: bool


# --------------------------------------------------------------------------- #
# Tool executors (task-bound so leak filtering can see the rubric)             #
# --------------------------------------------------------------------------- #
def _url_is_blocked(url: str) -> bool:
    low = url.lower()
    return any(domain in low for domain in DRACO_BLOCKED_DOMAINS)


def _result_leaks(task: DracoTask, *, url: str, title: str, text: str) -> bool:
    probe = ExaResult(
        title=title,
        url=url,
        published_date=None,
        author=None,
        highlights=(),
        text=text,
        fetched_text=None,
    )
    return _draco_search_result_leak_reason(task, probe) is not None


def make_web_search(task: DracoTask, exa_client: ExaSearchClient) -> Callable[[dict[str, Any]], str]:
    def run(args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return "Error: web_search requires a non-empty 'query'."
        num = args.get("num_results")
        num_results = num if isinstance(num, int) and 1 <= num <= 10 else DEFAULT_SEARCH_RESULTS
        bundle: ExaSearchBundle = exa_client.search_with_contents(
            query,
            exclude_domains=DRACO_BLOCKED_DOMAINS,
            num_results=num_results,
        )
        kept = [r for r in bundle.results if not _draco_search_result_leak_reason(task, r)]
        if not kept:
            return f"No usable results for query: {query!r}."
        lines = [f"web_search results for {query!r}:"]
        for i, r in enumerate(kept, start=1):
            extract = r.compact_text(max_chars=DEFAULT_SEARCH_RESULT_CHARS)
            date = f" ({r.published_date})" if r.published_date else ""
            lines.append(f"[{i}] {r.title}{date}\nURL: {r.url}\n{extract}")
        return "\n\n".join(lines)

    return run


def _markitdown_fetch(url: str, max_chars: int) -> str | None:
    """For filings/PDFs/spreadsheets, fetch the bytes (with a real UA) and convert
    via markitdown so tables survive. Returns None to fall back to plain text."""
    if _MARKITDOWN is None or not _is_fetchable_public_url(url):
        return None
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True, headers=_FETCH_HEADERS) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return None
        ext = _wants_markitdown(url, resp.headers.get("content-type", ""))
        if ext is None:
            return None
        result = _MARKITDOWN.convert_stream(io.BytesIO(resp.content[:_MAX_FETCH_BYTES]), file_extension=ext)
        text = (result.text_content or "").strip()
        return text[:max_chars] if text else None
    except Exception:  # noqa: BLE001 - fall back to the plain fetcher
        return None


# --- LlamaParse (cached) for high-fidelity table/PDF/filing parsing ---
LLAMAPARSE_BASE = "https://api.cloud.llamaindex.ai"
LLAMAPARSE_CACHE_DIR = Path(
    os.environ.get("LLAMAPARSE_CACHE_DIR", "artifacts/fusion-draco/llamaparse-cache")
)
_LLAMAPARSE_KEY: str | None = None
_LLAMAPARSE_KEY_LOADED = False


def _llamaparse_key() -> str | None:
    global _LLAMAPARSE_KEY, _LLAMAPARSE_KEY_LOADED
    if not _LLAMAPARSE_KEY_LOADED:
        from trusted_router.evals.fusion_live import load_eval_key

        _LLAMAPARSE_KEY = load_eval_key("LLAMAPARSE_API_KEY")
        _LLAMAPARSE_KEY_LOADED = True
    return _LLAMAPARSE_KEY


def _llamaparse_fetch(url: str, max_chars: int) -> str | None:
    """Parse table-heavy filings/PDFs/spreadsheets with LlamaParse -> markdown,
    caching the FULL parse on disk keyed by URL so each document is billed at most
    once (across re-runs, models, and tasks). Returns None to fall back to
    markitdown / plain text."""
    if not _is_fetchable_public_url(url):
        return None
    cache = LLAMAPARSE_CACHE_DIR / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.md"
    if cache.exists():
        try:
            cached = cache.read_text(encoding="utf-8").strip()
            return cached[:max_chars] if cached else None
        except Exception:  # noqa: BLE001
            pass
    key = _llamaparse_key()
    if not key:
        return None
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True, headers=_FETCH_HEADERS) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return None
        ext = _wants_markitdown(url, resp.headers.get("content-type", ""))
        if ext is None:
            return None  # only spend LlamaParse credits on table-heavy doc types
        content = resp.content[:_MAX_FETCH_BYTES]
        headers = {"Authorization": f"Bearer {key}", "accept": "application/json"}
        with httpx.Client(timeout=150.0) as client:
            up = client.post(
                f"{LLAMAPARSE_BASE}/api/v1/parsing/upload",
                headers=headers,
                files={"file": (f"doc{ext}", content, "application/octet-stream")},
            )
            if up.status_code != 200:
                return None
            job_id = (up.json() or {}).get("id")
            if not job_id:
                return None
            deadline = time.time() + 240.0
            status = ""
            while time.time() < deadline:
                s = client.get(f"{LLAMAPARSE_BASE}/api/v1/parsing/job/{job_id}", headers=headers)
                status = (s.json() or {}).get("status", "")
                if status in ("SUCCESS", "PARTIAL_SUCCESS", "ERROR", "CANCELED"):
                    break
                time.sleep(2.5)
            if status not in ("SUCCESS", "PARTIAL_SUCCESS"):
                return None
            md = client.get(
                f"{LLAMAPARSE_BASE}/api/v1/parsing/job/{job_id}/result/markdown", headers=headers
            )
            if md.status_code != 200:
                return None
            text = ((md.json() or {}).get("markdown") or "").strip()
        if not text:
            return None
        LLAMAPARSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(cache)
        return text[:max_chars]
    except Exception:  # noqa: BLE001 - fall back to markitdown/plain
        return None


def make_web_fetch(task: DracoTask, *, doc_parser: str = "llamaparse") -> Callable[[dict[str, Any]], str]:
    """doc_parser selects the table/PDF/filing parser chain (for ablation):
    'llamaparse' -> LlamaParse(cached) then markitdown then plain;
    'markitdown' -> markitdown then plain; 'plain' -> visible-text only."""
    def run(args: dict[str, Any]) -> str:
        url = str(args.get("url") or "").strip()
        if not url:
            return "Error: web_fetch requires a 'url'."
        if _url_is_blocked(url):
            return "Error: that domain is blocked for this task."
        text: str | None = None
        if doc_parser == "llamaparse":
            text = _llamaparse_fetch(url, DEFAULT_FETCH_CHARS)
        if not text and doc_parser in ("llamaparse", "markitdown"):
            text = _markitdown_fetch(url, DEFAULT_FETCH_CHARS)
        if not text:
            text = fetch_result_text(url, max_chars=DEFAULT_FETCH_CHARS)
        if not text:
            return f"Could not fetch readable content from {url}."
        if _result_leaks(task, url=url, title=url, text=text):
            return "Error: fetched content was blocked (benchmark-related)."
        return f"web_fetch content from {url}:\n{text}"

    return run


# --- sec_facts: exact as-filed figures from EDGAR XBRL (keyless, free) ---
SEC_DATA_BASE = "https://data.sec.gov"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_TICKER_MAP: "dict[str, str] | None" = None  # TICKER -> 10-digit CIK

# Per-metric us-gaap concept fallback chains (first that has data wins).
SEC_METRIC_CONCEPTS: dict[str, tuple[str, ...]] = {
    "operating_cash_flow": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ),
    "repurchases": ("PaymentsForRepurchaseOfCommonStock",),
    "dividends": ("PaymentsOfDividends", "PaymentsOfDividendsCommonStock"),
    "debt_issued": ("ProceedsFromIssuanceOfLongTermDebt", "ProceedsFromIssuanceOfDebt"),
    "debt_repaid": ("RepaymentsOfLongTermDebt", "RepaymentsOfDebt"),
    "share_based_comp": ("ShareBasedCompensation",),
    "stockholders_equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"),
}


def _sec_ticker_map() -> dict[str, str]:
    global _SEC_TICKER_MAP
    if _SEC_TICKER_MAP is None:
        out: dict[str, str] = {}
        try:
            with httpx.Client(timeout=30.0, headers=_FETCH_HEADERS) as c:
                data = c.get(SEC_TICKERS_URL).json()
            for row in (data.values() if isinstance(data, dict) else data):
                t = str(row.get("ticker", "")).upper().strip()
                cik = str(row.get("cik_str", "")).strip()
                if t and cik:
                    out[t] = cik.zfill(10)
        except Exception:  # noqa: BLE001
            pass
        _SEC_TICKER_MAP = out
    return _SEC_TICKER_MAP


def _sec_resolve_cik(ticker: str) -> str | None:
    t = ticker.strip().upper()
    if t.startswith("CIK"):
        t = t[3:]
    if t.isdigit():
        return t.zfill(10)
    return _sec_ticker_map().get(t)


def _sec_concept_values(cik: str, concept: str, *, fiscal_year: int | None, headers: dict[str, str]) -> list[dict[str, Any]]:
    url = f"{SEC_DATA_BASE}/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json"
    try:
        with httpx.Client(timeout=30.0, headers=headers) as c:
            r = c.get(url)
        if r.status_code != 200:
            return []
        units = (r.json() or {}).get("units") or {}
        usd = units.get("USD") or (next(iter(units.values()), []) if units else [])
        if fiscal_year:
            usd = [x for x in usd if x.get("fy") == fiscal_year]
        return usd
    except Exception:  # noqa: BLE001
        return []


def make_sec_facts(task: DracoTask) -> Callable[[dict[str, Any]], str]:
    def run(args: dict[str, Any]) -> str:
        ticker = str(args.get("ticker") or "").strip()
        if not ticker:
            return "Error: sec_facts requires a 'ticker' (symbol or 10-digit CIK)."
        cik = _sec_resolve_cik(ticker)
        if not cik:
            return f"Error: could not resolve '{ticker}' to a SEC CIK. Use the exact U.S. ticker or 10-digit CIK."
        fy_raw = args.get("fiscal_year")
        fy = int(fy_raw) if isinstance(fy_raw, int) or (isinstance(fy_raw, str) and fy_raw.isdigit()) else None
        metric = str(args.get("metric") or "").strip().lower()
        metrics = [metric] if metric in SEC_METRIC_CONCEPTS else list(SEC_METRIC_CONCEPTS)
        headers = {**_FETCH_HEADERS, "accept": "application/json"}
        header_line = f"SEC EDGAR XBRL facts for {ticker.upper()} (CIK {cik})" + (f", FY{fy}" if fy else "")
        lines = [header_line + ":"]
        found = False
        for mname in metrics:
            vals: list[dict[str, Any]] = []
            used = None
            for concept in SEC_METRIC_CONCEPTS[mname]:
                vals = _sec_concept_values(cik, concept, fiscal_year=fy, headers=headers)
                if vals:
                    used = concept
                    break
            if not vals:
                continue
            found = True
            vals = sorted(vals, key=lambda x: (str(x.get("end", "")), str(x.get("fp", ""))), reverse=True)
            lines.append(f"\n{mname} ({used}):")
            for item in vals[:6]:
                v = item.get("val")
                vs = f"{v:,}" if isinstance(v, (int, float)) else str(v)
                period = f"{item.get('start')}..{item.get('end')}" if item.get("start") else str(item.get("end"))
                lines.append(
                    f"  {vs} USD | period={period} fy={item.get('fy')} fp={item.get('fp')} form={item.get('form')}"
                )
        if not found:
            return (
                f"No US-GAAP XBRL facts found for {ticker.upper()} (CIK {cik}). "
                "It may be a foreign private issuer (20-F/IFRS) or not in XBRL — use web_fetch on the filing."
            )
        text = "\n".join(lines)[:MAX_TOOL_RESULT_CHARS]
        if _result_leaks(task, url=SEC_DATA_BASE, title="sec_facts", text=text):
            return "Error: result was blocked (benchmark-related)."
        return text

    return run


def make_bash(*, image: str = DEFAULT_BASH_IMAGE, timeout_seconds: float = 30.0) -> Callable[[dict[str, Any]], str]:
    docker = shutil.which("docker")

    def run(args: dict[str, Any]) -> str:
        command = str(args.get("command") or "").strip()
        if not command:
            return "Error: bash requires a 'command'."
        if docker is None:
            return "Error: bash sandbox unavailable (docker not found)."
        try:
            proc = subprocess.run(
                [
                    docker, "run", "--rm",
                    "--network", "none",
                    "--memory", "512m",
                    "--cpus", "1",
                    "--pids-limit", "256",
                    "-w", "/work",
                    image, "bash", "-lc", command,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return f"Error: command timed out after {timeout_seconds:.0f}s."
        out = (proc.stdout or "")[:6000]
        err = (proc.stderr or "")[:2000]
        parts = []
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        if not parts:
            parts.append("(no output)")
        return "\n".join(parts)

    return run


def build_tool_executors(
    task: DracoTask,
    *,
    exa_client: ExaSearchClient,
    bash_image: str = DEFAULT_BASH_IMAGE,
    bash_timeout_seconds: float = 30.0,
    enable_bash: bool = True,
    enable_sec_facts: bool = True,
    doc_parser: str = "llamaparse",
) -> tuple[list[dict[str, Any]], dict[str, Callable[[dict[str, Any]], str]]]:
    executors: dict[str, Callable[[dict[str, Any]], str]] = {
        "web_search": make_web_search(task, exa_client),
        "web_fetch": make_web_fetch(task, doc_parser=doc_parser),
    }
    enabled = {"web_search", "web_fetch"}
    if enable_sec_facts:
        executors["sec_facts"] = make_sec_facts(task)
        enabled.add("sec_facts")
    if enable_bash:
        executors["bash"] = make_bash(image=bash_image, timeout_seconds=bash_timeout_seconds)
        enabled.add("bash")
    schemas = [s for s in TOOL_SCHEMAS if s["function"]["name"] in enabled]
    return schemas, executors


# --------------------------------------------------------------------------- #
# Agentic loop                                                                #
# --------------------------------------------------------------------------- #
def _is_openai_reasoning_model(model: str) -> bool:
    # OpenAI reasoning models (gpt-5.x) require max_completion_tokens and reject temperature.
    return model.lower().removeprefix("openai/").startswith("gpt-5")


def _completion_body(
    model: str,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    temperature: float,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": model, "messages": messages}
    if _is_openai_reasoning_model(model):
        body["max_completion_tokens"] = max_tokens  # gpt-5.x: no temperature, max_completion_tokens
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = temperature
    if tools is not None:
        body["tools"] = tools
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    return body


def _post_with_retry(
    client: "tr_sdk.TrustedRouter", url: str, headers: dict[str, str], body: dict[str, Any], *, retries: int = 3
) -> "tr_sdk.SdkResponse":
    # The gateway call goes through the TrustedRouter SDK, which handles auth,
    # regional failover, and 429/5xx retries. url/headers are unused (the client
    # already carries the base URL and key) and kept only for signature stability.
    return tr_sdk.chat_response(client, body)


def run_agentic_completion(
    *,
    client: "tr_sdk.TrustedRouter",
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    tool_schemas: list[dict[str, Any]],
    executors: dict[str, Callable[[dict[str, Any]], str]],
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    max_tokens: int = 8_000,
    synthesis_max_tokens: int = DEFAULT_SYNTHESIS_MAX_TOKENS,
    temperature: float = 0.2,
    reasoning_effort: str | None = None,
    force_first_tool: bool = False,
) -> AgenticResult:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    records: list[ToolCallRecord] = []
    tool_calls_made = 0
    last_input = last_output = None
    started = time.perf_counter()
    steps = 0

    def record_usage(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal last_input, last_output
        usage = payload.get("usage") or {}
        if isinstance(usage.get("prompt_tokens"), int):
            last_input = usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            last_output = usage["completion_tokens"]
        return (payload.get("choices") or [{}])[0]

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    # --- Research phase: offer tools until the budget is reached or the model
    #     stops calling tools (a natural finish). ---
    while tool_calls_made < max_tool_calls:
        steps += 1
        # Force at least one tool call for models that otherwise answer from prior
        # knowledge (e.g. gemini-flash); switch to auto once engaged.
        body = _completion_body(
            model, messages, max_tokens=max_tokens, temperature=temperature,
            tools=tool_schemas,
            tool_choice="required" if (force_first_tool and tool_calls_made == 0) else "auto",
            reasoning_effort=reasoning_effort,
        )
        resp = _post_with_retry(client, url, headers, body)
        resp.raise_for_status()
        choice = record_usage(resp.json())
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            content = strip_tool_markup(msg.get("content") or "")
            if content.strip():
                # Natural finish: the model wrote a report without more tools.
                return AgenticResult(
                    content=content,
                    finish_reason=choice.get("finish_reason"),
                    tool_calls_made=tool_calls_made,
                    tool_records=records,
                    input_tokens=last_input,
                    output_tokens=last_output,
                    elapsed_ms=elapsed(),
                    steps=steps,
                    truncated_loop=False,
                )
            break  # empty content, no tool calls -> force a synthesis turn
        messages.append(
            {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls}
        )
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            executor = executors.get(name)
            if executor is None:
                result = f"Error: unknown tool {name!r}."
                records.append(ToolCallRecord(name=name, args=args, result_chars=0, error="unknown_tool"))
            else:
                try:
                    result = executor(args)
                    records.append(ToolCallRecord(name=name, args=args, result_chars=len(result)))
                except Exception as exc:  # noqa: BLE001 - surface tool error to the model
                    result = f"Error running {name}: {exc}"
                    records.append(
                        ToolCallRecord(name=name, args=args, result_chars=0, error=type(exc).__name__)
                    )
            tool_calls_made += 1
            messages.append(
                {"role": "tool", "tool_call_id": tc.get("id"), "content": result[:MAX_TOOL_RESULT_CHARS]}
            )

    # --- Synthesis phase: budget reached (or empty natural finish). One final
    #     call with NO tools and a strong write-the-report instruction, so the
    #     model writes a clean report instead of leaking tool-call markup. ---
    steps += 1
    messages.append({"role": "user", "content": SYNTHESIS_INSTRUCTION})
    body = _completion_body(
        model, messages, max_tokens=synthesis_max_tokens, temperature=temperature,
        reasoning_effort=reasoning_effort,
    )
    resp = _post_with_retry(client, url, headers, body)
    resp.raise_for_status()
    choice = record_usage(resp.json())
    msg = choice.get("message") or {}
    return AgenticResult(
        content=strip_tool_markup(msg.get("content") or ""),
        finish_reason=choice.get("finish_reason"),
        tool_calls_made=tool_calls_made,
        tool_records=records,
        input_tokens=last_input,
        output_tokens=last_output,
        elapsed_ms=elapsed(),
        steps=steps,
        truncated_loop=True,
    )
