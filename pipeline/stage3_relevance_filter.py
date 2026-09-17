import logging
from claude_agent_sdk import query, AssistantMessage, TextBlock
from utils.claude_options import isolated_options
from utils.concurrency import gather_bounded
from utils.json_helpers import parse_json_response
from config import MAX_CONCURRENT_RELEVANCE_CALLS, MODEL_FAST as MODEL
from search.mcp_config import (
    PUBMED_SERVER_NAME,
    PUBMED_TOOL_FETCH_FULLTEXT,
    PUBMED_TOOL_FIND_RELATED,
    pubmed_mcp_server,
)

logger = logging.getLogger(__name__)

_SYSTEM_TEMPLATE = """You are a medical relevance rater for a physician's personalized research digest.

Physician profile:
- Name: {name}
- Specialties: {specialties}
- Subspecialties: {subspecialties}
- Role: {role}
- Practice setting: {practice_setting}
- Country: {country}
- Patient population: {patient_population}
- Research interests: {research_interests}

Score this paper's relevance to this physician from 1 to 10:

10: Directly addresses their specialty AND research interests; high-evidence; immediately actionable
8-9: Highly relevant to specialty or subspecialty; well-designed study
6-7: Relevant to general practice area; useful background knowledge
4-5: Tangentially related; general medicine without specialty focus
1-3: Outside their specialty; non-clinical (basic science only); irrelevant

Also rate clinical actionability:
- "high": directly changes practice (new drug, new protocol, guideline update)
- "moderate": informs practice without immediate change
- "low": background knowledge, basic science, conceptual

Consider their practice setting: academic physicians benefit more from methodological nuance;
community physicians value practical applicability over research implications.

TOOLS AVAILABLE:
You have access to two PubMed tools. Most papers can be scored from the abstract alone —
only reach for these when relevance is genuinely unclear from the abstract:
- pubmed_find_related: checks whether this paper is part of a more (or less) relevant
  cluster of work — e.g. a pivotal trial vs. one of many similar small studies.
- pubmed_fetch_fulltext: pulls full text when the abstract doesn't show enough to judge
  actionability for this specific physician.
Call these tools as needed, then give your final answer. Your FINAL message must be
ONLY the JSON object below — no commentary, no markdown, in the same or a later turn
than any tool calls you make.

Respond with valid JSON, no markdown, no preamble:
{{
  "relevance_score": 8,
  "relevance_reasoning": "Direct RCT on heart failure with reduced ejection fraction — core subspecialty.",
  "clinical_actionability": "high"
}}"""

_DEFAULTS = {
    "relevance_score": 5,
    "relevance_reasoning": "processing error",
    "clinical_actionability": "moderate",
}

# Below the >= 5 keep threshold: with no abstract there is nothing to judge actionability
# on. Still returned in all_scored, so the paper is remembered as seen.
NO_ABSTRACT_SCORE = {
    "relevance_score": 3,
    "relevance_reasoning": "No abstract available; not scored.",
    "clinical_actionability": "low",
}


def _build_system_prompt(profile: dict) -> str:
    prefs = profile.get("newsletter_preferences", {})
    return _SYSTEM_TEMPLATE.format(
        name=profile.get("name", ""),
        specialties=", ".join(profile.get("specialties", [])) or "not specified",
        subspecialties=", ".join(profile.get("subspecialties", [])) or "none",
        role=", ".join(profile.get("role", [])) or "not specified",
        practice_setting=profile.get("practice_setting", "not specified"),
        country=profile.get("country", "not specified"),
        patient_population=profile.get("patient_population", "not specified"),
        research_interests=", ".join(profile.get("research_interests", [])) or "none",
    )


async def score_paper(paper: dict, system_prompt: str) -> dict:
    """Scores a single paper. Never raises — returns defaults on any failure.
    No abstract: NO_ABSTRACT_SCORE without a Claude call."""
    if not (paper.get("abstract") or "").strip():
        return {**paper, **NO_ABSTRACT_SCORE}

    prompt = (
        f"PMID: {paper.get('pmid', '')}\n"
        f"Title: {paper.get('title', '')}\n"
        f"Journal: {paper.get('journal', '')}\n"
        f"Study design: {paper.get('study_design', 'unknown')}\n"
        f"Evidence level: {paper.get('evidence_level', 'C')}\n"
        f"Source specialty search: {paper.get('source_specialty', '')}\n"
        f"Abstract:\n{paper.get('abstract', '')}\n\n"
        f"Score this paper's relevance."
    )

    options = isolated_options(
        system_prompt=system_prompt,
        mcp_servers={PUBMED_SERVER_NAME: pubmed_mcp_server()},
        allowed_tools=[PUBMED_TOOL_FETCH_FULLTEXT, PUBMED_TOOL_FIND_RELATED],
        max_turns=4,
        model=MODEL,
    )
    # Multi-turn now: keep only the *last* assistant turn's text, mirroring stage2.
    last_text_blocks: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                text_blocks = [block.text for block in msg.content if isinstance(block, TextBlock)]
                if text_blocks:
                    last_text_blocks = text_blocks
    except Exception as e:
        logger.warning(f"Relevance scorer failed for PMID {paper.get('pmid', '?')}: {e}")
        return {**paper, **_DEFAULTS}

    raw = "\n".join(last_text_blocks)
    try:
        scored = parse_json_response(raw)
    except ValueError as e:
        logger.warning(f"JSON parse failed for PMID {paper.get('pmid', '?')}: {e}")
        return {**paper, **_DEFAULTS}

    return {
        **paper,
        "relevance_score": int(scored.get("relevance_score", _DEFAULTS["relevance_score"])),
        "relevance_reasoning": scored.get("relevance_reasoning", ""),
        "clinical_actionability": scored.get("clinical_actionability", _DEFAULTS["clinical_actionability"]),
    }


async def filter_papers(papers: list[dict], profile: dict) -> tuple[list[dict], list[dict]]:
    """
    Scores all papers, discards those with relevance_score < 5,
    sorts descending, caps at max_papers.

    Returns (kept, all_scored): `kept` is the capped, sorted list that continues
    through the rest of the pipeline; `all_scored` is every paper that was actually
    scored this run (including ones discarded by the threshold or the cap), so the
    caller can record all of them as "seen" — evaluating a paper is real work we
    don't want to repeat on every run just because it didn't make the final digest.
    """
    system_prompt = _build_system_prompt(profile)
    max_papers = profile.get("newsletter_preferences", {}).get("max_papers", 10)

    scored = await gather_bounded(
        lambda p: score_paper(p, system_prompt), papers, MAX_CONCURRENT_RELEVANCE_CALLS
    )

    result = []
    for paper, s in zip(papers, scored):
        if isinstance(s, Exception):
            logger.warning(f"Unexpected scoring error for PMID {paper.get('pmid', '?')}: {s}")
            result.append({**paper, **_DEFAULTS})
        else:
            result.append(s)

    kept = [p for p in result if p.get("relevance_score", 0) >= 5]
    kept.sort(key=lambda p: p.get("relevance_score", 0), reverse=True)
    kept = kept[:max_papers]

    print(f"  Relevance filter: {len(papers)} → {len(kept)} papers (discarded {len(papers) - len(kept)})")
    return kept, result
