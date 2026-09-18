import logging

from config import MAX_CONCURRENT_TRIALS_CALLS
from search.clinicaltrials_mcp_client import find_matching_trials
from utils.concurrency import gather_bounded

logger = logging.getLogger(__name__)


async def crosscheck_trials(papers: list[dict]) -> list[dict]:
    """Cross-checks each top-relevance paper (already filtered by stage 3) against
    ClinicalTrials.gov, attaching a `matching_trials` list to each paper. Never drops
    a paper — a lookup failure just leaves matching_trials empty for that paper."""
    results = await gather_bounded(
        lambda p: find_matching_trials(p), papers, MAX_CONCURRENT_TRIALS_CALLS
    )

    crosschecked = []
    n_with_trials = 0
    for paper, result in zip(papers, results):
        if isinstance(result, Exception):
            logger.warning(f"Unexpected trials cross-check error for PMID {paper.get('pmid', '?')}: {result}")
            crosschecked.append({**paper, "matching_trials": []})
        else:
            if result:
                n_with_trials += 1
            crosschecked.append({**paper, "matching_trials": result})

    print(f"  Trials cross-check: {n_with_trials}/{len(papers)} papers have matching registered trials")
    return crosschecked
