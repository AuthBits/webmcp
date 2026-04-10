"""
webmcp - MCP server for web scraping and content extraction
"""

import asyncio
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from ddgs import DDGS
from markdownify import markdownify as md
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from playwright.async_api import async_playwright
from readability import Document as ReadabilityDocument
from starlette.middleware.cors import CORSMiddleware

# ============================================================================
# Configuration
# ============================================================================

logger = logging.getLogger(__name__)

TOOL_CALL_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tool_calls.log.json"
)


def _load_dotenv(path: str) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ if missing."""
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue

                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        logger.warning(f"Failed to load .env file from {path}: {e}")


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

LLM_URL = os.environ.get("LLM_URL", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "ddg").strip().lower()
SEARXNG_URL = os.environ.get("SEARXNG_URL", "").strip()

if not LLM_URL or not LLM_MODEL:
    raise ValueError("LLM_URL and LLM_MODEL environment variables are required")

# ============================================================================
# Content Processing
# ============================================================================


def _html_to_clean(html: str) -> str:
    """Convert HTML to clean markdown, collapsing excessive whitespace."""
    text = md(
        html,
        heading_style="ATX",
        strip=["img", "script", "style", "nav", "footer", "header"]
    )
    # Collapse runs of 3+ blank lines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse runs of spaces (but not newlines) on each line
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.strip()


async def _fetch_one(browser: Any, url: str, timeout_ms: int = 0) -> tuple[str, str]:
    """Fetch a single URL using an existing browser instance."""
    page = await browser.new_page()
    await page.set_extra_http_headers({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(2000)
        html = await page.content()
    finally:
        await page.close()

    doc = ReadabilityDocument(html)
    title = doc.title()
    clean_text = _html_to_clean(doc.summary())

    if len(clean_text) < 50:
        clean_text = _html_to_clean(html)

    return title, clean_text


async def _fetch_pages(urls: list[str]) -> list[tuple[str, str, str | None]]:
    """Fetch multiple URLs in parallel with a shared browser. Returns [(title, text, error)]."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            async def _fetch_single(url: str) -> tuple[str, str, str | None]:
                try:
                    title, text = await _fetch_one(browser, url)
                    return title, text, None
                except Exception as e:
                    logger.error(f"Failed to fetch {url}: {e}")
                    return "", "", str(e)

            results = await asyncio.gather(*[_fetch_single(u) for u in urls])
        finally:
            await browser.close()

    return results


async def _fetch_page_light(url: str) -> tuple[str, str]:
    """Fast fetch without a browser — good for simple pages."""
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        verify=False
    ) as client:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        html = resp.text

    doc = ReadabilityDocument(html)
    title = doc.title()
    clean_text = _html_to_clean(doc.summary())

    if len(clean_text) < 50:
        clean_text = _html_to_clean(html)

    return title, clean_text


async def _llm_extract(content: str, prompt: str | None, schema: dict | None) -> str:
    """Send content to local LLM for structured extraction."""
    system_msg = (
        "You are a data extraction assistant. "
        "Extract the requested information from the provided web page content. "
        "Be precise and only return the extracted data. Be as detailed as possible "
        "without including extra information. Do not skimp. "
        "NEVER return an empty result. If you cannot find the requested data, "
        "you MUST explain why — e.g. the page didn't contain it, the content was "
        "blocked, the page was a login wall, etc."
    )

    if schema:
        system_msg += f"\n\nReturn the data as JSON matching this schema:\n{json.dumps(schema, indent=2)}"

    user_msg = content
    if prompt:
        user_msg += f"\n\n---\nExtraction request: {prompt}"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{LLM_URL}/v1/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.1,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        resp.raise_for_status()
        result = resp.json()
        return result["choices"][0]["message"]["content"]


async def _search_ddg(query: str, limit: int) -> list[dict]:
    """Search using DuckDuckGo."""
    results = DDGS().text(query, max_results=limit)
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href", ""),
            "description": r.get("body", ""),
        }
        for r in results
    ]


async def _search_searxng(query: str, limit: int) -> list[dict]:
    """Search using a SearXNG instance."""
    if not SEARXNG_URL:
        raise ValueError("SEARXNG_URL is required when SEARCH_PROVIDER=searxng")

    base_url = SEARXNG_URL.rstrip("/")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(
            f"{base_url}/search",
            params={"q": query, "format": "json"},
            headers={"User-Agent": "webmcp/1.0"},
        )
        resp.raise_for_status()
        payload = resp.json()

    results = payload.get("results", [])[:limit]
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("content", ""),
        }
        for r in results
    ]

# ============================================================================
# Tool Call Logging
# ============================================================================


class ToolCallLogger:
    """Manages persistent tool call logging with bounded history."""

    MAX_ENTRIES = 10

    def __init__(self, log_path: str):
        self.log_path = Path(log_path)
        self._buffer: list[dict[str, Any]] = []
        self._load_existing()

    def _load_existing(self) -> None:
        """Load existing log on startup."""
        if self.log_path.exists():
            try:
                with open(self.log_path, "r") as f:
                    self._buffer = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load existing log: {e}")
                self._buffer = []

    def _flush(self) -> None:
        """Persist the buffer to disk."""
        try:
            with open(self.log_path, "w") as f:
                json.dump(self._buffer[-self.MAX_ENTRIES:], f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Failed to flush tool log: {e}")

    def log_call(self, tool_name: str, arguments: dict, result: str) -> None:
        """Log a tool call and persist if buffer is full."""
        entry = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name,
            "arguments": arguments,
            "result": result,
        }
        self._buffer.append(entry)

        if len(self._buffer) > self.MAX_ENTRIES:
            self._buffer = self._buffer[-self.MAX_ENTRIES:]
            self._flush()


_tool_logger = ToolCallLogger(TOOL_CALL_LOG_PATH)

# ============================================================================
# MCP Server Setup
# ============================================================================

mcp = FastMCP(
    "webmcp",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


@mcp.tool()
async def get_current_date() -> str:
    """Get the current date. Use this tool to get today's date in ISO format (YYYY-MM-DD)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d (%A)")


@mcp.tool()
async def search_web(query: str, limit: int = 10) -> str:
    """Searches the web for a query. Returns titles, URLs, and descriptions."""
    if SEARCH_PROVIDER == "searxng":
        data = await _search_searxng(query, limit)
    elif SEARCH_PROVIDER == "ddg":
        data = await _search_ddg(query, limit)
    else:
        raise ValueError("SEARCH_PROVIDER must be either 'ddg' or 'searxng'")

    _tool_logger.log_call(
        "search_web",
        {"query": query, "limit": limit, "provider": SEARCH_PROVIDER},
        json.dumps(data)
    )
    return json.dumps(data, indent=2)


@mcp.tool()
async def extract(
    urls: list[str],
    prompt: str | None = None,
    schema: dict | None = None,
    use_browser: bool = True,
) -> str:
    """Extract structured data from one or more URLs using a local LLM.

    Fetches each URL, extracts readable content, then sends it to a local LLM
    with your prompt/schema to pull out structured data.

    To find URLs first, call search_web separately, then pass the results here.

    Args:
        urls: URLs to extract from.
        prompt: Tells the extraction LLM what data to pull from the page content.
        schema: JSON schema the output should conform to.
        use_browser: If True (default), use Playwright for JS rendering.
                     False uses lightweight HTTP fetch.
    """
    if not prompt and not schema:
        error_result = {"error": "At least one of prompt or schema is required."}
        _tool_logger.log_call("extract", {"urls": urls}, json.dumps(error_result))
        return json.dumps(error_result, indent=2)

    # Fetch and clean each page
    contents: list[str] = []

    if use_browser:
        results = await _fetch_pages(urls)
        for url, (title, text, err) in zip(urls, results):
            if err:
                contents.append(f"=== {url} ===\nFailed to fetch: {err}")
            else:
                if len(text) > 12000:
                    text = text[:12000] + "\n... [truncated]"
                contents.append(f"=== {url} ===\n{title}\n\n{text}")
    else:
        for url in urls:
            try:
                title, text = await _fetch_page_light(url)
                if len(text) > 12000:
                    text = text[:12000] + "\n... [truncated]"
                contents.append(f"=== {url} ===\n{title}\n\n{text}")
            except Exception as e:
                contents.append(f"=== {url} ===\nFailed to fetch: {e}")

    combined = "\n\n".join(contents)
    result = await _llm_extract(combined, prompt, schema)

    _tool_logger.log_call(
        "extract",
        {
            "urls": urls,
            "prompt": prompt,
            "schema": schema,
            "use_browser": use_browser,
        },
        result
    )

    return result


# ============================================================================
# FastAPI App Setup
# ============================================================================

app = mcp.streamable_http_app()

app = CORSMiddleware(
    app,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id"],
)


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8642)
