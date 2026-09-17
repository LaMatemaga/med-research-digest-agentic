import json
import logging

from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from search import mcp_tool_runner
from search.clinicaltrials_mcp_client import extract_trials, find_matching_trials
from search.mcp_config import CLINICALTRIALS_TOOL_SEARCH
from search.mcp_tool_runner import ToolCallResult
from tests.fixtures import mcp_payloads as fx

PAPER = {"pmid": "40000001", "title": "SGLT2 in HF", "mesh_terms": ["Heart Failure"]}


def test_extract_trials_from_real_markdown():
    trials = extract_trials(ToolCallResult(text=fx.TRIALS_MARKDOWN))

    assert trials == [
        {
            "nct_id": "NCT04008394",
            "title": "Dapagliflozin in Heart Failure With Preserved Ejection Fraction",
            "status": "COMPLETED",
            "phase": "PHASE3",
            "url": "https://clinicaltrials.gov/study/NCT04008394",
        },
        {
            "nct_id": "NCT01234567",
            "title": "Heart Failure Registry",
            "status": "RECRUITING",
            "phase": "",
            "url": "https://clinicaltrials.gov/study/NCT01234567",
        },
    ]


def test_extract_trials_from_structured_content_uses_phases_array():
    [trial] = extract_trials(ToolCallResult(structured=fx.TRIALS_STRUCTURED))

    assert trial["nct_id"] == "NCT04008394"
    assert trial["phase"] == "PHASE3"


def test_no_matches_is_empty_without_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert extract_trials(ToolCallResult(text="No studies matched the search criteria.")) == []
    assert not caplog.records


def test_unparseable_payload_logs_raw_payload(caplog):
    with caplog.at_level(logging.WARNING):
        assert extract_trials(ToolCallResult(text="something unexpected"), "40000001") == []
    assert any("something unexpected" in r.getMessage() for r in caplog.records)


async def test_find_matching_trials_end_to_end_with_json_text_envelope(monkeypatch):
    async def _query(*, prompt, options):
        assert "conditionQuery: Heart Failure" in prompt
        yield AssistantMessage(
            content=[ToolUseBlock(id="c", name=CLINICALTRIALS_TOOL_SEARCH, input={})], model="m"
        )
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="c", content=[{"type": "text", "text": json.dumps(fx.TRIALS_STRUCTURED)}])]
        )

    monkeypatch.setattr(mcp_tool_runner, "query", _query)

    trials = await find_matching_trials(PAPER)

    assert [t["nct_id"] for t in trials] == ["NCT04008394"]


async def test_find_matching_trials_caps_at_max_results(monkeypatch):
    async def _query(*, prompt, options):
        yield AssistantMessage(
            content=[ToolUseBlock(id="c", name=CLINICALTRIALS_TOOL_SEARCH, input={})], model="m"
        )
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="c", content=[{"type": "text", "text": fx.TRIALS_MARKDOWN}])]
        )

    monkeypatch.setattr(mcp_tool_runner, "query", _query)

    trials = await find_matching_trials(PAPER, max_results=1)

    assert [t["nct_id"] for t in trials] == ["NCT04008394"]


# --- query builder: always valid Essie free text --------------------------------------
# Inputs below reproduced live against clinicaltrials.gov/api/v2/studies?query.cond=:
# the long title -> 400 "Too complicated query"; the bracketed title -> 400
# "extraneous input '['". Builder output for each was confirmed to return 200.

import re

import pytest

from search.clinicaltrials_mcp_client import MAX_QUERY_WORDS, _build_condition_query

LONG_TITLE = (
    "Effect of empagliflozin on cardiac remodeling in patients with heart failure and preserved "
    "ejection fraction: a randomized, double-blind, placebo-controlled trial"
)
HOSTILE_TITLE = 'Deep brain stimulation OR levodopa for Parkinson\'s disease: "real-world" outcomes (DBS-PD) [NOT a trial]'


def _assert_valid_essie_free_text(q: str):
    assert q, "empty query"
    assert not re.search(r"[\[\]]", q), "stray bracket outside AREA[]/RANGE[]"
    assert not re.search(r'["(){}:;,/\\]', q)
    assert not re.search(r"\b(AND|OR|NOT)\b", q), "uppercase boolean operator"
    assert len(q.split()) <= MAX_QUERY_WORDS


@pytest.mark.parametrize(
    "paper,expected",
    [
        ({"mesh_terms": ["Heart Failure"]}, "Heart Failure"),
        ({"mesh_terms": ["Adult", "Humans", "Female", "Carcinoma, Non-Small-Cell Lung"]}, "Carcinoma Non-Small-Cell Lung"),
        ({"mesh_terms": ["Diabetes Mellitus, Type 2"]}, "Diabetes Mellitus Type 2"),
        ({"mesh_terms": [], "title": LONG_TITLE}, "empagliflozin cardiac remodeling heart failure"),
        ({"mesh_terms": [], "title": "[Efficacy of metformin in type 2 diabetes]."}, "Efficacy metformin type diabetes"),
        ({"mesh_terms": [], "title": HOSTILE_TITLE}, "Deep brain stimulation levodopa Parkinson"),
        ({"mesh_terms": ["Humans", "Aged"], "title": "SARS-CoV-2 / COVID-19 mRNA vaccine safety"}, "SARS-CoV-2 COVID-19 mRNA vaccine safety"),
    ],
)
def test_condition_query_is_valid_and_meaningful(paper, expected):
    q = _build_condition_query(paper)
    _assert_valid_essie_free_text(q)
    assert q == expected


def test_condition_query_empty_when_nothing_usable():
    assert _build_condition_query({"mesh_terms": ["Humans"], "title": "[ ]: of the and"}) == ""


async def test_find_matching_trials_sends_sanitized_query_and_skips_empty(monkeypatch):
    prompts = []

    async def _query(*, prompt, options):
        prompts.append(prompt)
        return
        yield

    monkeypatch.setattr(mcp_tool_runner, "query", _query)

    assert await find_matching_trials({"pmid": "9", "mesh_terms": [], "title": LONG_TITLE}) == []
    assert await find_matching_trials({"pmid": "10", "mesh_terms": [], "title": "[ ]"}) == []

    [prompt] = prompts  # no call at all for the unusable one
    sent = re.search(r"conditionQuery: (.*)", prompt).group(1)
    _assert_valid_essie_free_text(sent)
