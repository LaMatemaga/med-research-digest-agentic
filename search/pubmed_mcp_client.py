"""PubMed access via the @cyanheads/pubmed-mcp-server MCP server, driven by the
Claude Agent SDK. Replaces the old raw httpx calls against NCBI E-utilities
(esearch/efetch) and the hand-rolled XML parser — search/xml_parser.py is retired.
"""

import logging
import os

from claude_agent_sdk import ClaudeAgentOptions

from config import MODEL_FAST as MODEL
from search.mcp_config import (
    PUBMED_SERVER_NAME,
    PUBMED_TOOL_FETCH,
    PUBMED_TOOL_SEARCH,
    pubmed_mcp_server,
)
from search.mcp_tool_runner import run_and_collect_tool_results

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a PubMed research assistant with access to PubMed MCP tools.

Given a search specification, you MUST, in order:
1. Call pubmed_search_articles with the given query and maxResults to get matching PMIDs.
2. Call pubmed_fetch_articles with all PMIDs returned by the search (respecting the
   tool's per-call limit) to retrieve full metadata (title, authors, journal, abstract,
   publication types, MeSH terms).
3. Once both calls are done, reply with the single word DONE.

Do not summarize, reformat, or transcribe the tool results yourself — the caller reads
the raw tool output, not your prose. Do not call any other tool."""


class PubMedMCPClient:
    """Async context manager mirroring the old PubMedClient's interface, so the
    discovery stage's calling code barely changes even though fetching now happens
    through agentic MCP tool calls instead of direct HTTP requests."""

    def __init__(self):
        self.api_key = os.getenv("NCBI_API_KEY")

    async def __aenter__(self) -> "PubMedMCPClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def search_and_fetch(self, query_spec: dict) -> list[dict]:
        """Full round-trip via MCP tools: search -> fetch -> PaperRecord dicts."""
        specialty = query_spec["specialty"]
        pubmed_query = query_spec["query"]
        max_results = query_spec["retmax"]

        prompt = (
            f"Search query: {pubmed_query}\n"
            f"maxResults: {max_results}\n\n"
            "Run the PubMed search and fetch tool calls now."
        )
        options = ClaudeAgentOptions(
            system_prompt=_SYSTEM_PROMPT,
            mcp_servers={PUBMED_SERVER_NAME: pubmed_mcp_server()},
            allowed_tools=[PUBMED_TOOL_SEARCH, PUBMED_TOOL_FETCH],
            max_turns=6,
            model=MODEL,
        )

        tool_results = await run_and_collect_tool_results(prompt, options)

        pmids = _extract_pmids(tool_results.get(PUBMED_TOOL_SEARCH))
        articles = _extract_articles(tool_results.get(PUBMED_TOOL_FETCH))

        if pmids and not articles:
            logger.warning(
                f"PubMed MCP: search found {len(pmids)} PMIDs for '{specialty}' "
                "but pubmed_fetch_articles returned no articles"
            )

        return [_to_paper_record(article, specialty) for article in articles]


def _first(d: dict, *keys, default=None):
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return default


def _extract_pmids(payload) -> list[str]:
    """Tolerates a few plausible response shapes for the search tool's result."""
    if not payload:
        return []
    if isinstance(payload, list):
        return [str(p) for p in payload if p]
    if isinstance(payload, dict):
        candidates = _first(payload, "pmids", "ids", default=[])
        if isinstance(candidates, dict):
            candidates = candidates.get("idlist", [])
        if not candidates:
            results = payload.get("results") or payload.get("articles") or []
            candidates = [_first(item, "pmid", "id") for item in results if isinstance(item, dict)]
        return [str(p) for p in candidates if p]
    return []


def _extract_articles(payload) -> list[dict]:
    """Tolerates a few plausible response shapes for the fetch tool's result."""
    if not payload:
        return []
    if isinstance(payload, list):
        return [a for a in payload if isinstance(a, dict)]
    if isinstance(payload, dict):
        articles = _first(payload, "articles", "results", "records", default=[])
        return [a for a in articles if isinstance(a, dict)]
    return []


def _to_paper_record(article: dict, source_specialty: str) -> dict:
    pmid = str(_first(article, "pmid", "PMID", "id", default="")).strip()

    authors = _first(article, "authors", "authorList", default=[]) or []
    if authors and isinstance(authors[0], dict):
        authors = [_first(a, "name", "fullName", default="") for a in authors]
    authors = [a for a in authors if a]

    return {
        "pmid": pmid,
        "title": _first(article, "title", "articleTitle", default=""),
        "authors": authors,
        "journal": _first(article, "journal", "journalTitle", default=""),
        "pub_date": _first(article, "pubDate", "pub_date", "publicationDate", default=""),
        "abstract": _first(article, "abstract", "abstractText", default=""),
        "publication_types": _first(article, "publicationTypes", "publication_types", default=[]) or [],
        "mesh_terms": _first(article, "meshTerms", "mesh_terms", default=[]) or [],
        "source_specialty": source_specialty,
        "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
    }
