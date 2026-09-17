"""Clinical trial cross-checks via the clinicaltrialsgov-mcp-server MCP server.

Tool schema read from the server's source (src/mcp-server/tools/definitions/
search-studies.tool.ts): clinicaltrials_search_studies input `{conditionQuery,
interventionQuery, pageSize (default 10), ...}`; structuredContent `{studies:
[{nctId, briefTitle, overallStatus, phases: string[], enrollmentCount, leadSponsor,
conditions, locations}], totalCount, nextPageToken}`; content[] markdown with one
`- **NCT…**: title [STATUS]` line per study, followed by an indented
`  PHASE3 | N=… | sponsor | conditions` meta line. Either form may reach us (see
search/mcp_tool_runner.py), so both are parsed.
"""

import logging
import re

from utils.claude_options import isolated_options

from config import MODEL_FAST as MODEL
from search.mcp_config import (
    CLI_ENV,
    CLINICALTRIALS_SERVER_NAME,
    CLINICALTRIALS_TOOL_SEARCH,
    clinicaltrials_mcp_server,
)
from search.mcp_tool_runner import ToolCallResult, run_and_collect_tool_results

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a clinical trials lookup assistant with access to the
ClinicalTrials.gov MCP tool clinicaltrials_search_studies.

Given a paper's condition, call clinicaltrials_search_studies exactly once with
conditionQuery set to that condition verbatim — do not add brackets, quotes, AND/OR/NOT or
extra words — and pageSize set to the requested number. Then reply
with the single word DONE. Do not summarize the results yourself — the caller reads the
raw tool output."""


async def find_matching_trials(paper: dict, max_results: int = 3) -> list[dict]:
    """Cross-checks one paper against ClinicalTrials.gov by condition.
    Never raises — returns [] on any failure or when nothing usable matches."""
    condition_query = _build_condition_query(paper)
    if not condition_query:
        return []

    prompt = (
        f"conditionQuery: {condition_query}\n"
        f"pageSize: {max_results}\n\n"
        "Run the clinicaltrials_search_studies tool call now."
    )
    options = isolated_options(
        system_prompt=_SYSTEM_PROMPT,
        mcp_servers={CLINICALTRIALS_SERVER_NAME: clinicaltrials_mcp_server()},
        allowed_tools=[CLINICALTRIALS_TOOL_SEARCH],
        max_turns=3,
        model=MODEL,
        env=CLI_ENV,
    )

    try:
        tool_results = await run_and_collect_tool_results(prompt, options)
    except Exception as e:
        logger.warning(f"Clinical trials cross-check failed for PMID {paper.get('pmid', '?')}: {e}")
        return []

    trials: list[dict] = []
    for result in tool_results.get(CLINICALTRIALS_TOOL_SEARCH, []):
        trials.extend(extract_trials(result, paper.get("pmid", "?")))
    return trials[:max_results]


# Checked live against https://clinicaltrials.gov/api/v2/studies?query.cond=… (the call
# the MCP server makes), not assumed:
# - `[` / `]` outside AREA[…]/RANGE[…] -> 400 "extraneous input '['". PubMed wraps
#   translated titles in brackets ("[Efficacy of …].").
# - ~20+ words -> 400 "Too complicated query" (a full title used as a fallback hit this).
# - uppercase AND/OR/NOT are operators ("heart failure OR diabetes" returned 1000x more).
# - `, : ; /` and matched `( )` parse fine, but every extra word is ANDed, so a long
#   query also collapses to 0-2 matches well before it errors.
MAX_QUERY_WORDS = 5
_QUERY_UNSAFE_RE = re.compile(r"[^A-Za-z0-9\- ]+")
_BOOLEAN_WORDS = {"and", "or", "not"}
_TITLE_STOPWORDS = _BOOLEAN_WORDS | {
    "a", "an", "the", "of", "in", "on", "for", "with", "without", "to", "from", "by", "at", "as",
    "vs", "versus", "among", "after", "before", "during", "between", "into", "via", "its", "their",
    "is", "are", "was", "were", "be", "effect", "effects", "impact", "association", "associated",
    "study", "trial", "randomized", "randomised", "controlled", "double-blind", "placebo-controlled",
    "cohort", "retrospective", "prospective", "systematic", "review", "meta-analysis", "analysis",
    "patients", "adults", "children", "case", "report", "evaluation", "outcomes", "results",
}
# MeSH check tags / generic descriptors: never a useful trial condition.
_GENERIC_MESH = {
    "humans", "animals", "male", "female", "adult", "aged", "aged, 80 and over", "middle aged",
    "young adult", "adolescent", "child", "child, preschool", "infant", "infant, newborn",
    "pregnancy", "retrospective studies", "prospective studies", "cohort studies",
    "treatment outcome", "risk factors", "double-blind method", "follow-up studies",
    "cross-sectional studies", "time factors", "surveys and questionnaires", "united states",
}


def _build_condition_query(paper: dict) -> str:
    """A conditionQuery that is always valid Essie free text: the first non-generic MeSH
    descriptor, else keywords from the title, as at most MAX_QUERY_WORDS plain words (no
    brackets, no uppercase boolean operators). Empty when nothing usable is left."""
    for term in paper.get("mesh_terms", []):
        if term.strip().lower() not in _GENERIC_MESH and (query := _sanitize_query(term)):
            return query
    title_words = [
        w for w in _sanitize_query(paper.get("title", ""), max_words=None).split()
        if w.lower() not in _TITLE_STOPWORDS and len(w) > 1
    ]
    return " ".join(title_words[:MAX_QUERY_WORDS])


def _sanitize_query(text: str, max_words: int | None = MAX_QUERY_WORDS) -> str:
    words = [
        w.strip("-") for w in _QUERY_UNSAFE_RE.sub(" ", text).split()
        if w.strip("-") and w.lower() not in _BOOLEAN_WORDS
    ]
    return " ".join(words if max_words is None else words[:max_words])


def extract_trials(result: ToolCallResult, pmid: str = "?") -> list[dict]:
    """Normalized trial dicts from one clinicaltrials_search_studies call. Never raises."""
    if result.is_error:
        return []
    try:
        structured = result.structured
        if isinstance(structured, dict) and isinstance(structured.get("studies"), list):
            return [_trial_from_structured(s) for s in structured["studies"] if isinstance(s, dict)]
        trials = _trials_from_markdown(result.text)
    except Exception as e:
        logger.warning(
            f"Clinical trials: parsing search_studies output for PMID {pmid} raised "
            f"{type(e).__name__}: {e}. Raw payload: {result.text!r}"
        )
        return []
    if not trials and result.text and "No studies matched" not in result.text:
        logger.warning(
            f"Clinical trials: could not parse search_studies output for PMID {pmid}. "
            f"Raw payload (first 1000 chars): {result.text[:1000]!r}"
        )
    return trials


def _trial_from_structured(study: dict) -> dict:
    nct_id = str(study.get("nctId", "")).strip()
    return _trial_record(nct_id, study.get("briefTitle", ""), study.get("overallStatus", ""), study.get("phases") or [])


_STUDY_LINE_RE = re.compile(
    r"^- \*\*(?P<nct>NCT\d{8})\*\*(?:: (?P<title>.*?))?(?: \[(?P<status>[A-Z_]+)\])?$", re.MULTILINE
)
_PHASE_TOKEN_RE = re.compile(r"^(?:EARLY_)?PHASE\d|^NA$")


def _trials_from_markdown(text: str) -> list[dict]:
    lines = text.splitlines()
    trials = []
    for i, line in enumerate(lines):
        match = _STUDY_LINE_RE.match(line)
        if not match:
            continue
        phases: list[str] = []
        if i + 1 < len(lines) and lines[i + 1].startswith("  "):
            first_meta = lines[i + 1].strip().split(" | ")[0]
            if all(_PHASE_TOKEN_RE.match(p) for p in first_meta.split("/")):
                phases = first_meta.split("/")
        trials.append(_trial_record(match["nct"], match["title"] or "", match["status"] or "", phases))
    return trials


def _trial_record(nct_id: str, title: str, status: str, phases: list[str]) -> dict:
    return {
        "nct_id": nct_id,
        "title": title,
        "status": status,
        "phase": "/".join(phases),
        "url": f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
    }
