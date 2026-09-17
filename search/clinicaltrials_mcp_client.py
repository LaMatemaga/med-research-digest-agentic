"""Clinical trial cross-checks via the clinicaltrialsgov-mcp-server MCP server."""

import logging

from claude_agent_sdk import ClaudeAgentOptions

from config import MODEL_FAST as MODEL
from search.mcp_config import (
    CLINICALTRIALS_SERVER_NAME,
    CLINICALTRIALS_TOOL_SEARCH,
    clinicaltrials_mcp_server,
)
from search.mcp_tool_runner import run_and_collect_tool_results

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a clinical trials lookup assistant with access to the
ClinicalTrials.gov MCP tool clinicaltrials_search_studies.

Given a paper's condition and (if provided) intervention, call clinicaltrials_search_studies
exactly once with conditionQuery and, if given, interventionQuery, requesting only the top
matches. Then reply with the single word DONE. Do not summarize the results yourself —
the caller reads the raw tool output."""


async def find_matching_trials(paper: dict, max_results: int = 3) -> list[dict]:
    """Cross-checks one paper against ClinicalTrials.gov by condition/intervention.
    Never raises — returns [] on any failure or when nothing usable matches."""
    condition_query = _build_condition_query(paper)
    if not condition_query:
        return []

    prompt = (
        f"conditionQuery: {condition_query}\n"
        f"pageSize: {max_results}\n\n"
        "Run the clinicaltrials_search_studies tool call now."
    )
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM_PROMPT,
        mcp_servers={CLINICALTRIALS_SERVER_NAME: clinicaltrials_mcp_server()},
        allowed_tools=[CLINICALTRIALS_TOOL_SEARCH],
        max_turns=3,
        model=MODEL,
    )

    try:
        tool_results = await run_and_collect_tool_results(prompt, options)
    except Exception as e:
        logger.warning(f"Clinical trials cross-check failed for PMID {paper.get('pmid', '?')}: {e}")
        return []

    studies = _extract_studies(tool_results.get(CLINICALTRIALS_TOOL_SEARCH))
    return [_to_trial_record(s) for s in studies[:max_results]]


def _build_condition_query(paper: dict) -> str:
    """Derives a condition query from MeSH terms, falling back to the title."""
    mesh_terms = paper.get("mesh_terms", [])
    if mesh_terms:
        return mesh_terms[0]
    return paper.get("title", "")[:120]


def _first(d: dict, *keys, default=None):
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return default


def _extract_studies(payload) -> list[dict]:
    if not payload:
        return []
    if isinstance(payload, list):
        return [s for s in payload if isinstance(s, dict)]
    if isinstance(payload, dict):
        studies = _first(payload, "studies", "results", default=[])
        return [s for s in studies if isinstance(s, dict)]
    return []


def _to_trial_record(study: dict) -> dict:
    nct_id = str(_first(study, "nctId", "nct_id", "id", default="")).strip()
    return {
        "nct_id": nct_id,
        "title": _first(study, "briefTitle", "title", default=""),
        "status": _first(study, "overallStatus", "status", default=""),
        "phase": _first(study, "phase", default=""),
        "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
    }
