"""Sandbox-side HTTP fetch and document-to-text helper for ``draco_full``.

The immutable AnyEval fetch image installs this module at
``/opt/draco/fetch_helper.py``. Network access is possible only through the proxy
selected below; the worker never performs the request itself.

Extraction mirrors the standalone harness: MarkItDown is attempted only for the
PDF/spreadsheet/SEC documents selected by ``_wants_markitdown``; all other responses
take the plain-text path. LlamaParse is intentionally disabled in ``draco_full``.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

MAX_BODY_BYTES = 8_000_000
MAX_TEXT_CHARS = 25_000
FETCH_HEADERS = {
    "User-Agent": "TrustedRouter-Research research@quillrouter.com",
    "Accept": "*/*",
}


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self._title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"}:
            self._hidden += 1
        elif normalized == "title":
            self._title = True
        elif not self._hidden and normalized in {"tr", "p", "li", "br", "div"}:
            self.text_parts.append("\n")
        elif not self._hidden and normalized in {"td", "th"}:
            self.text_parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style", "noscript", "svg"} and self._hidden:
            self._hidden -= 1
        elif normalized == "title":
            self._title = False
        elif not self._hidden and normalized in {"tr", "p", "li"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._hidden or not data.strip():
            return
        if self._title:
            self.title_parts.append(data)
        self.text_parts.append(data)


def _proxy_for(scheme: str) -> str:
    names = (
        (
            "HTTPS_PROXY",
            "https_proxy",
            "HTTP_PROXY",
            "http_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
        if scheme == "https"
        else (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        )
    )
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise RuntimeError("HTTP(S) proxy is required")


def _wants_markitdown(url: str, content_type: str) -> str | None:
    """Match the standalone harness's table-heavy document selection."""
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


def _plain_text(body: bytes, content_type: str) -> tuple[str, str]:
    charset = "utf-8"
    match = re.search(r"charset=([^;\s]+)", content_type, flags=re.IGNORECASE)
    if match:
        charset = match.group(1).strip("\"'")
    decoded = body.decode(charset, errors="replace")
    if "html" not in content_type.lower() and "<html" not in decoded[:500].lower():
        return "", _normalize_visible_text(decoded)
    parser = _ReadableHTML()
    parser.feed(decoded)
    title = " ".join(" ".join(parser.title_parts).split())
    text = _normalize_visible_html_text("".join(parser.text_parts))
    return title, text


def _normalize_visible_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalize_visible_html_text(text: str) -> str:
    lines = [_normalize_visible_text(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _truncate_plain_text(text: str) -> str:
    return (
        text[: MAX_TEXT_CHARS - 1].rstrip() + "…"
        if len(text) > MAX_TEXT_CHARS
        else text
    )


def _plain_fetch(body: bytes, content_type: str) -> tuple[str, str]:
    title, text = _plain_text(body, content_type)
    return title, _truncate_plain_text(text)


def _extract(body: bytes, url: str, content_type: str) -> tuple[str, str]:
    extension = _wants_markitdown(url, content_type)
    if extension is None:
        return _plain_fetch(body, content_type)
    try:
        from markitdown import MarkItDown

        converted = MarkItDown().convert_stream(
            io.BytesIO(body), file_extension=extension
        )
        text = str(getattr(converted, "text_content", "") or "").strip()
        title = str(getattr(converted, "title", "") or "").strip()
        if text:
            return title, text[:MAX_TEXT_CHARS]
    except Exception:  # noqa: BLE001 - deterministic plain-text fallback
        return _plain_fetch(body, content_type)
    return _plain_fetch(body, content_type)


def fetch(url: str) -> dict[str, str | int]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http(s) URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL must contain a host")
    proxy = _proxy_for(parsed.scheme)
    with (
        httpx.Client(
            proxy=proxy,
            trust_env=False,
            follow_redirects=True,
            timeout=30.0,
            headers=FETCH_HEADERS,
        ) as client,
        client.stream("GET", url) as response,
    ):
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            remaining = MAX_BODY_BYTES - size
            if remaining <= 0:
                break
            chunks.append(chunk[:remaining])
            size += min(len(chunk), remaining)
            if len(chunk) > remaining:
                break
        body = b"".join(chunks)
        final_url = str(response.url)
        content_type = response.headers.get("content-type", "")
        status = response.status_code
    title, text = _extract(body, final_url, content_type)
    return {
        "url": final_url,
        "title": title,
        "text": text,
        "content_type": content_type,
        "status": status,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fetch_helper.py URL", file=sys.stderr)
        return 2
    try:
        payload = fetch(argv[1])
    except Exception as exc:  # noqa: BLE001 - report a concise sandbox tool error
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
