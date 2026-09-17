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
