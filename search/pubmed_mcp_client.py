"""PubMed access via the @cyanheads/pubmed-mcp-server MCP server, driven by the
Claude Agent SDK. Replaces the old raw httpx calls against NCBI E-utilities
(esearch/efetch) and the hand-rolled XML parser — search/xml_parser.py is retired.

Tool schemas below were read from the server's source (v2.10.13, the version `npx
@latest` resolves to; GitHub `main` and the published npm tarball match):
- pubmed_search_articles input `{query, maxResults, ...}`; structuredContent
  `{query, offset, pmids: string[], summaries, searchUrl}`; content[] markdown with a
  `**PMIDs:** 1, 2, 3` line.
- pubmed_fetch_articles input `{pmids: string[1..200], includeMesh, maxResponseCharacters}`;
  structuredContent `{articles: FetchedArticle[], totalReturned, unavailablePmids?}`;
  content[] markdown with one `### <title>` section per article.
Either form may reach us (see search/mcp_tool_runner.py), so both are parsed.
"""

import logging
import os
import re

from utils.claude_options import isolated_options

from config import MODEL_FAST as MODEL
from search.mcp_config import (
    CLI_ENV,
    PUBMED_SERVER_NAME,
    PUBMED_TOOL_FETCH,
    PUBMED_TOOL_SEARCH,
    pubmed_mcp_server,
)
from search.mcp_tool_runner import ToolCallResult, run_and_collect_tool_results

logger = logging.getLogger(__name__)

FETCH_BATCH_SIZE = 10

_SYSTEM_PROMPT = f"""You are a PubMed research assistant with access to PubMed MCP tools.

Given a search specification, you MUST, in order:
1. Call pubmed_search_articles with the given query and maxResults.
2. Call pubmed_fetch_articles for the PMIDs the search returned, passing them as the
   `pmids` array, at most {FETCH_BATCH_SIZE} PMIDs per call. Make as many calls as needed
   to cover every PMID.
3. Once every PMID has been fetched, reply with the single word DONE.

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
        max_results = query_spec["retmax"]

        prompt = (
            f"Search query: {query_spec['query']}\n"
            f"maxResults: {max_results}\n\n"
            "Run the PubMed search and fetch tool calls now."
        )
        n_fetch_batches = -(-max_results // FETCH_BATCH_SIZE)
        options = isolated_options(
            system_prompt=_SYSTEM_PROMPT,
            mcp_servers={PUBMED_SERVER_NAME: pubmed_mcp_server()},
            allowed_tools=[PUBMED_TOOL_SEARCH, PUBMED_TOOL_FETCH],
            max_turns=n_fetch_batches + 4,
            model=MODEL,
            env=CLI_ENV,
        )

        tool_results = await run_and_collect_tool_results(prompt, options)

        pmids: list[str] = []
        for result in tool_results.get(PUBMED_TOOL_SEARCH, []):
            pmids.extend(extract_pmids(result, specialty))

        articles: list[dict] = []
        for result in tool_results.get(PUBMED_TOOL_FETCH, []):
            articles.extend(extract_articles(result, specialty))

        if pmids and not articles:
            n_calls = len(tool_results.get(PUBMED_TOOL_FETCH, []))
            logger.warning(
                f"PubMed MCP: search found {len(pmids)} PMIDs for '{specialty}' but "
                f"{n_calls} pubmed_fetch_articles call(s) yielded no parseable articles "
                "(warnings above show why; --debug-mcp logs every raw payload)"
            )

        return _dedup_by_pmid([_to_paper_record(a, specialty) for a in articles])


# --- search ------------------------------------------------------------------------

_PMIDS_LINE_RE = re.compile(r"\*\*PMIDs:\*\*\s*([0-9][0-9,\s]*)")


def extract_pmids(result: ToolCallResult, specialty: str = "") -> list[str]:
    if result.is_error:
        return []
    structured = result.structured
    if isinstance(structured, dict) and isinstance(structured.get("pmids"), list):
        return [str(p) for p in structured["pmids"] if p]
    match = _PMIDS_LINE_RE.search(result.text)
    if match:
        return [p.strip() for p in match.group(1).split(",") if p.strip()]
    if result.text and "No results" not in result.text and "**Returned:** 0" not in result.text:
        _warn_unparsed("pubmed_search_articles", specialty, result.text)
    return []


# --- fetch -------------------------------------------------------------------------


def extract_articles(result: ToolCallResult, specialty: str = "") -> list[dict]:
    """Normalized article dicts from one pubmed_fetch_articles call. Never raises —
    a parse failure is logged with the raw payload rather than silently becoming []."""
    if result.is_error:
        return []
    try:
        structured = result.structured
        if isinstance(structured, dict) and isinstance(structured.get("articles"), list):
            return [_article_from_structured(a) for a in structured["articles"] if isinstance(a, dict)]
        articles = _articles_from_markdown(result.text)
    except Exception as e:
        logger.warning(
            f"PubMed MCP: parsing pubmed_fetch_articles output for '{specialty}' raised "
            f"{type(e).__name__}: {e}. Raw payload: {result.text!r}"
        )
        return []
    if not articles and "**Articles Returned:** 0" not in result.text:
        _warn_unparsed("pubmed_fetch_articles", specialty, result.text)
    return articles


def _article_from_structured(a: dict) -> dict:
    """Maps the real FetchedArticleSchema (fetch-articles.tool.ts) to our shape."""
    authors = []
    for au in a.get("authors") or []:
        if au.get("collectiveName"):
            authors.append(au["collectiveName"])
        elif au.get("lastName"):
            authors.append(f"{au['lastName']} {au.get('initials', '')}".strip())
    journal_info = a.get("journalInfo") or {}
    date = journal_info.get("publicationDate") or {}
    pub_date = date.get("medlineDate") or "-".join(
        p for p in (date.get("year"), date.get("month"), date.get("day")) if p
    )
    return {
        "pmid": str(a.get("pmid", "")),
        "title": a.get("title", ""),
        "authors": authors,
        "journal": journal_info.get("title", ""),
        "pub_date": pub_date,
        "abstract": a.get("abstractText", ""),
        "publication_types": a.get("publicationTypes") or [],
        "mesh_terms": [m["descriptorName"] for m in a.get("meshTerms") or [] if m.get("descriptorName")],
        "pubmed_url": a.get("pubmedUrl", ""),
    }


_ARTICLE_SPLIT_RE = re.compile(r"\n### ")
_UNESCAPE_RE = re.compile(r"\\([\\*_\[\]<`~])")
_PMID_RE = re.compile(r"^\*\*PMID:\*\*\s*(\S+)", re.MULTILINE)
_PUBMED_URL_RE = re.compile(r"^\*\*PubMed:\*\*\s*(\S+)", re.MULTILINE)
_JOURNAL_RE = re.compile(r"^\*\*Journal:\*\*\s*(.+)$", re.MULTILINE)
_TYPES_RE = re.compile(r"^\*\*Type:\*\*\s*(.+)$", re.MULTILINE)
_AUTHORS_RE = re.compile(r"^\*\*Authors \(\d+\):\*\*\n((?:- .*\n?)+)", re.MULTILINE)
_ABSTRACT_RE = re.compile(r"^#### Abstract\n(.+?)(?=\n\n\*\*Keywords:|\n\n#### |\Z)", re.MULTILINE | re.DOTALL)
_MESH_RE = re.compile(r"^#### MeSH Terms\n((?:- .*\n?)+)", re.MULTILINE)
_MESH_DESCRIPTOR_RE = re.compile(r"^- (.+?)(?: \[[^\]]*\])?(?: \(major\))?(?: \(.*\))?$")
_AUTHOR_TRAILER_RE = re.compile(r" (\([A-Za-z]+\)|\[aff [\d,]+\]|· ORCID \S+)")
_JOURNAL_DATE_RE = re.compile(r"\b((?:19|20)\d{2}(?: [A-Za-z]{3,9})?(?: \d{1,2})?)\b")


def _articles_from_markdown(text: str) -> list[dict]:
    chunks = _ARTICLE_SPLIT_RE.split(text)[1:]  # [0] is the "## PubMed Articles" preamble
    return [_article_from_chunk(chunk) for chunk in chunks]


def _article_from_chunk(chunk: str) -> dict:
    title_line, _, body = chunk.partition("\n")
    article: dict = {"title": _UNESCAPE_RE.sub(r"\1", title_line.strip())}

    if m := _PMID_RE.search(body):
        article["pmid"] = m.group(1)
    if m := _PUBMED_URL_RE.search(body):
        article["pubmed_url"] = m.group(1)
    if m := _TYPES_RE.search(body):
        article["publication_types"] = [t.strip() for t in m.group(1).split(",") if t.strip()]
    if m := _AUTHORS_RE.search(body):
        article["authors"] = [
            _AUTHOR_TRAILER_RE.split(line[2:], maxsplit=1)[0].strip()
            for line in m.group(1).splitlines()
            if line.startswith("- ")
        ]
    if m := _ABSTRACT_RE.search(body):
        article["abstract"] = m.group(1).strip()
    if m := _MESH_RE.search(body):
        article["mesh_terms"] = [
            d.group(1).strip() for line in m.group(1).splitlines() if (d := _MESH_DESCRIPTOR_RE.match(line))
        ]
    if m := _JOURNAL_RE.search(body):
        journal_line = m.group(1)
        article["journal"] = journal_line.split(", ")[0]
        if d := _JOURNAL_DATE_RE.search(journal_line):
            article["pub_date"] = d.group(1)
    return article


# --- shared ------------------------------------------------------------------------


def _warn_unparsed(tool: str, specialty: str, text: str) -> None:
    hint = ""
    if "exceeds maximum allowed tokens" in text or "MAX_MCP_OUTPUT_TOKENS" in text:
        hint = " The CLI truncated an oversized MCP response — lower FETCH_BATCH_SIZE."
    logger.warning(
        f"PubMed MCP: could not parse {tool} output for '{specialty}'.{hint} "
        f"Raw payload (first 1000 chars): {text[:1000]!r}"
    )


def _dedup_by_pmid(papers: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for p in papers:
        if p["pmid"] and p["pmid"] not in seen:
            seen.add(p["pmid"])
            out.append(p)
    return out


def _to_paper_record(article: dict, source_specialty: str) -> dict:
    pmid = str(article.get("pmid", "")).strip()
    return {
        "pmid": pmid,
        "title": article.get("title", ""),
        "authors": article.get("authors", []),
        "journal": article.get("journal", ""),
        "pub_date": article.get("pub_date", ""),
        "abstract": article.get("abstract", ""),
        "publication_types": article.get("publication_types", []),
        "mesh_terms": article.get("mesh_terms", []),
        "source_specialty": source_specialty,
        "url": article.get("pubmed_url") or (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""),
    }
