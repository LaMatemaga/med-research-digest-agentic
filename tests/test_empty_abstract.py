"""A paper with no abstract is an expected PubMed data-quality case: no Claude call, a
sensible default, and no warning."""

import logging

import pipeline.stage2_evidence_grader as stage2
import pipeline.stage3_relevance_filter as stage3


def _no_call(calls: list):
    async def _query(*, prompt, options):
        calls.append(prompt)
        raise AssertionError("Claude must not be called for a paper without an abstract")
        yield

    return _query


async def test_grading_skips_claude_for_missing_or_blank_abstract(monkeypatch, caplog):
    calls: list = []
    monkeypatch.setattr(stage2, "query", _no_call(calls))
    papers = [{"pmid": "41771074", "abstract": ""}, {"pmid": "2", "abstract": "   \n"}, {"pmid": "3"}]

    with caplog.at_level(logging.WARNING):
        graded = await stage2.grade_all(papers)

    assert calls == []
    assert not caplog.records
    for p in graded:
        assert p["evidence_level"] == "C"
        assert p["study_design"] == "unknown"
        assert p["methodology_flags"] == ["no abstract"]
        assert p["grader_reasoning"] == "No abstract available; not graded."


async def test_relevance_skips_claude_and_scores_below_keep_threshold(monkeypatch, caplog):
    calls: list = []
    monkeypatch.setattr(stage3, "query", _no_call(calls))

    with caplog.at_level(logging.WARNING):
        kept, all_scored = await stage3.filter_papers([{"pmid": "41771074", "abstract": ""}], {})

    assert calls == []
    assert not caplog.records
    assert kept == []
    [scored] = all_scored  # still recorded as seen by main.py
    assert scored["relevance_score"] < 5
    assert scored["clinical_actionability"] == "low"


async def test_papers_with_abstracts_still_graded(monkeypatch):
    from claude_agent_sdk import AssistantMessage, TextBlock

    async def _query(*, prompt, options):
        yield AssistantMessage(content=[TextBlock(text='{"study_design": "RCT", "evidence_level": "A"}')], model="m")

    monkeypatch.setattr(stage2, "query", _query)

    [graded] = await stage2.grade_all([{"pmid": "1", "abstract": "A randomized trial."}])

    assert graded["evidence_level"] == "A"
