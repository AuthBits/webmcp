# webmcp

`webmcp` is an MCP server for web search and content extraction. LLM agents can use it to:

- search the web with DuckDuckGo (default) or SearXNG (optional)
- fetch and clean page content from one or more URLs
- send cleaned content to a local LLM for structured extraction

## Features

- `search_web(query, limit=10)` returns web results (title, URL, description)
- `extract(urls, prompt=None, schema=None, use_browser=True)` extracts data from pages
- browser-based fetching with Playwright for JavaScript-heavy sites
- lightweight HTTP fetching mode for faster/simple pages
- persistent tool-call logging to `tool_calls.log.json`
- configurable search provider: DDG by default, optional SearXNG

## Critical Requirement

For the main researcher llama.cpp server, include `--webui-mcp-proxy` in launch parameters. Without this flag, this workflow will not function correctly.

## Prompting And Tested Setup

For best results, use `research_prompt.txt` as your system prompt. This prompt is a core part of the intended workflow and quality; it is effectively half of how this repository is meant to function.

Tested setup:

- Main researcher LLM: `Qwen3.5:27b-Q3_K_M.gguf` via llama.cpp on an RTX 4090, context length 200,000, about 40 tok/s.
- Extract tool LLM: `Qwen3.5:9b-Q4_K_M.gguf` via llama.cpp on a GTX 1080 Ti, context length 32,768, about 40 tok/s.
- This workflow has been tested with the llama.cpp WebUI specifically, and has not been validated with other MCP clients yet.

## Requirements

- Python 3.10+
- A local OpenAI-compatible LLM endpoint (for example, llama.cpp, LM Studio, vLLM, ollama, etc)

## Configuration

The app reads LLM settings from environment variables and supports a local `.env` file.

1. Copy `.env.example` to `.env`
2. Set values:

```env
LLM_URL=http://localhost:1234
LLM_MODEL=your-model-name
SEARCH_PROVIDER=ddg
# Optional when SEARCH_PROVIDER=searxng
SEARXNG_URL=http://localhost:8080
```

`LLM_URL` and `LLM_MODEL` are required at startup.
`SEARCH_PROVIDER` defaults to `ddg`. Set it to `searxng` to replace DDG, and provide `SEARXNG_URL`.

## Search Providers

`search_web` supports two providers:

- `ddg` (default): uses DuckDuckGo via `ddgs`
- `searxng`: uses your SearXNG instance

SearXNG notes:

- Set `SEARCH_PROVIDER=searxng`
- Set `SEARXNG_URL` to your instance base URL (for example, `http://192.168.0.55:8888`)
- `webmcp` calls `<SEARXNG_URL>/search` with `format=json`

## Install

Install dependencies from the pinned requirements file:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Run

```bash
python app.py
```

Server starts on:

- `http://0.0.0.0:8642`

## MCP Usage Notes

- `extract(..., use_browser=True)` is best for dynamic pages that require JS rendering.
- `extract(..., use_browser=False)` is faster for static pages.
- If extraction quality is poor, the LLM should provide a more specific `prompt` and/or a stricter `schema`.

## TODO

- Revisit JS page rendering and extraction strategy. Right now, roughly 25-30% of pages return little or no usable content even when fetched successfully.
- Improve anti-bot handling for page fetches. Many targets still return 400-range errors, so investigate stronger browser mimicry (Playwright/Chromium behavior, headers, fingerprinting, and potentially user-agent/profile rotation).

## License

MIT. See `LICENSE`.