"""
Web search — only ever called when the network status says online (see
main.py's dynamic tool list), so this respects your offline toggle
automatically rather than needing its own separate check everywhere.

DuckDuckGo search needs no API key, which matters for a local-first app
— no account, no key to leak, works the same for anyone who downloads
Nela. httpx + BeautifulSoup for actually reading a result page is the
standard, genuinely production-grade combination for this — not a
lightweight stand-in, this is what real scraping pipelines use.
"""

from typing import Optional

import httpx
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

from schemas import ToolResult

MAX_RESULTS = 5
MAX_PAGE_CHARS = 4000
REQUEST_TIMEOUT = 8


def web_search(query: str) -> ToolResult:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=MAX_RESULTS))
    except Exception as e:
        return ToolResult(success=False, message=f"Web search failed: {e}")

    if not results:
        return ToolResult(success=False, message=f"No results found for '{query}'.")

    summary_lines = []
    for r in results:
        title = r.get("title", "")
        body = r.get("body", "")
        href = r.get("href", "")
        summary_lines.append(f"- {title}: {body} ({href})")

    return ToolResult(
        success=True,
        message=f"Found {len(results)} results for '{query}'.",
        data="\n".join(summary_lines),
    )


def fetch_page_text(url: str) -> Optional[str]:
    """Fetches a URL and extracts readable text — used when a search result
    alone isn't enough and the model needs the actual page content."""
    try:
        resp = httpx.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    text = " ".join(soup.get_text(separator=" ").split())
    return text[:MAX_PAGE_CHARS]
