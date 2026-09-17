import logging
from claude_agent_sdk import query, AssistantMessage, TextBlock
from utils.claude_options import isolated_options
from utils.concurrency import gather_bounded
from utils.json_helpers import parse_json_response
from config import MAX_CONCURRENT_GRADING_CALLS, MODEL_FAST as MODEL
from search.mcp_config import (
    PUBMED_SERVER_NAME,
    PUBMED_TOOL_FETCH_FULLTEXT,
    PUBMED_TOOL_FIND_RELATED,
    pubmed_mcp_server,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a medical evidence grader. You receive a research paper's metadata and abstract.

Your job is to classify the study design, assign an evidence level, and flag methodological concerns.

EVIDENCE LEVELS:

Level A (Strong):
- Systematic review or meta-analysis of RCTs
- Large multi-center RCT (n > 500, pre-registered)
- Well-designed cohort study with hard outcomes and long follow-up

Level B (Moderate):
- Single RCT of any size, especially pilot or single-center
- Prospective cohort study
- High-quality case-control study
- Systematic review of observational studies

Level C (Low / Early):
- Case series (< 30 patients) or case reports
- Cross-sectional study
- Expert opinion, editorial, commentary
- Pilot or feasibility study
- Animal or in vitro study
- Conference abstract or preprint without peer review

STUDY DESIGN classification (choose exactly one):
- "meta-analysis": publication type or title contains meta-analysis or systematic review
- "RCT": publication type = Randomized Controlled Trial, or title/abstract contains "randomized"
- "cohort": abstract contains cohort, prospective, retrospective + study
- "case-control": abstract contains case-control
- "case-series": abstract contains case series, or n < 30 patients described
- "pilot": abstract contains pilot, feasibility, proof of concept
- "opinion": publication type = Editorial, Comment, Letter, or Opinion
- "review": publication type = Review without meta-analysis
- "unknown": cannot determine from abstract alone

METHODOLOGY FLAGS (include all that apply):
- "small sample": n < 50 in intervention arm
- "no control group": no comparator described
- "industry funding": funded by pharmaceutical or device company
- "single center": only one institution
- "short follow-up": < 6 months for a chronic disease outcome
- "surrogate outcome": primary outcome is a biomarker, not a clinical endpoint
- "unregistered trial": RCT without a registration number
- "preprint": not yet peer-reviewed

TOOLS AVAILABLE:
You have access to two PubMed tools. Most papers can be graded from the abstract
alone — only reach for these when the abstract genuinely leaves the grade ambiguous:
- pubmed_fetch_fulltext: retrieves full text (methods/results) when the abstract omits
  sample size, randomization, blinding, or funding details you need to pick a level.
- pubmed_find_related: finds related articles (trial registration, companion paper,
  a later meta-analysis) when that context changes the grade.
Call these tools as needed, then give your final answer. Your FINAL message must be
ONLY the JSON object below — no commentary, no markdown, in the same or a later turn
than any tool calls you make.

Respond with valid JSON, no markdown, no preamble:
{
  "study_design": "RCT",
  "evidence_level": "B",
  "methodology_flags": ["small sample", "single center"],
  "grader_reasoning": "Single-center RCT with n=47. Evidence level B: RCT but limited by small sample size and single site."
}"""

_DEFAULTS = {
    "study_design": "unknown",
    "evidence_level": "C",
    "methodology_flags": [],
    "grader_reasoning": "processing error",
}


NO_ABSTRACT_GRADE = {
    "study_design": "unknown",
    "evidence_level": "C",
    "methodology_flags": ["no abstract"],
    "grader_reasoning": "No abstract available; not graded.",
}


def has_abstract(paper: dict) -> bool:
    return bool((paper.get("abstract") or "").strip())


async def grade_paper(paper: dict) -> dict:
    """Grades a single paper. Never raises — returns defaults on any failure.
    A paper with no abstract is an expected data-quality case (PubMed has records
    without one): it gets NO_ABSTRACT_GRADE without a Claude call, which would only
    refuse to grade it."""
    if not has_abstract(paper):
        return {**paper, **NO_ABSTRACT_GRADE}

    prompt = (
        f"PMID: {paper.get('pmid', '')}\n"
        f"Title: {paper.get('title', '')}\n"
        f"Journal: {paper.get('journal', '')}\n"
        f"Publication Date: {paper.get('pub_date', '')}\n"
        f"Publication Types: {', '.join(paper.get('publication_types', []))}\n"
        f"Abstract:\n{paper.get('abstract', '')}\n\n"
        f"Grade this paper."
    )

    options = isolated_options(
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={PUBMED_SERVER_NAME: pubmed_mcp_server()},
        allowed_tools=[PUBMED_TOOL_FETCH_FULLTEXT, PUBMED_TOOL_FIND_RELATED],
        max_turns=4,
        model=MODEL,
    )
    # Multi-turn now: keep only the *last* assistant turn's text, since earlier turns
    # may contain no text (pure tool calls) or shouldn't be mixed with the final JSON.
    last_text_blocks: list[str] = []
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                text_blocks = [block.text for block in msg.content if isinstance(block, TextBlock)]
                if text_blocks:
                    last_text_blocks = text_blocks
    except Exception as e:
        logger.warning(f"Claude grader failed for PMID {paper.get('pmid', '?')}: {e}")
        return {**paper, **_DEFAULTS}

    raw = "\n".join(last_text_blocks)
    try:
        grade = parse_json_response(raw)
    except ValueError as e:
        logger.warning(f"JSON parse failed for PMID {paper.get('pmid', '?')}: {e}")
        return {**paper, **_DEFAULTS}

    return {
        **paper,
        "study_design": grade.get("study_design", _DEFAULTS["study_design"]),
        "evidence_level": grade.get("evidence_level", _DEFAULTS["evidence_level"]),
        "methodology_flags": grade.get("methodology_flags", []),
        "grader_reasoning": grade.get("grader_reasoning", ""),
    }


async def grade_all(papers: list[dict]) -> list[dict]:
    """Grades papers with at most MAX_CONCURRENT_GRADING_CALLS in flight. Same order."""
    n_no_abstract = sum(not has_abstract(p) for p in papers)
    if n_no_abstract:
        print(f"  {n_no_abstract} paper(s) have no abstract — not graded (evidence level C)")
    results = await gather_bounded(grade_paper, papers, MAX_CONCURRENT_GRADING_CALLS)
    graded = []
    for paper, result in zip(papers, results):
        if isinstance(result, Exception):
            logger.warning(f"Unexpected error grading PMID {paper.get('pmid', '?')}: {result}")
            graded.append({**paper, **_DEFAULTS})
        else:
            graded.append(result)
    return graded
