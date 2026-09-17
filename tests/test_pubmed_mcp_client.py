import json

import pytest
from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from search import mcp_tool_runner
from search.mcp_config import PUBMED_TOOL_FETCH, PUBMED_TOOL_SEARCH
from search.pubmed_mcp_client import PubMedMCPClient


def _fake_query_factory(search_result: dict, fetch_result: dict):
    """Builds a fake claude_agent_sdk.query() that mimics the SDK executing exactly
    the two MCP tool calls we expect, without spawning any subprocess or hitting
    the network."""

    async def _fake_query(*, prompt, options):
        yield AssistantMessage(
            content=[ToolUseBlock(id="call_1", name=PUBMED_TOOL_SEARCH, input={})],
            model="claude-haiku-4-5-20251001",
        )
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="call_1", content=json.dumps(search_result))]
        )
        yield AssistantMessage(
            content=[ToolUseBlock(id="call_2", name=PUBMED_TOOL_FETCH, input={})],
            model="claude-haiku-4-5-20251001",
        )
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="call_2", content=json.dumps(fetch_result))]
        )

    return _fake_query


@pytest.fixture
def query_spec():
    return {"specialty": "cardiology", "query": "Heart Failure[MeSH]", "retmax": 30}


async def test_search_and_fetch_parses_tool_results_into_paper_records(monkeypatch, query_spec):
    search_result = {"pmids": ["111", "222"]}
    fetch_result = {
        "articles": [
            {
                "pmid": "111",
                "title": "A trial of X",
                "authors": ["Smith J", "Doe A"],
                "journal": "NEJM",
                "pubDate": "2026-03",
                "abstract": "Background... Results...",
                "publicationTypes": ["Randomized Controlled Trial"],
                "meshTerms": ["Heart Failure"],
            }
        ]
    }
    monkeypatch.setattr(mcp_tool_runner, "query", _fake_query_factory(search_result, fetch_result))

    async with PubMedMCPClient() as client:
        papers = await client.search_and_fetch(query_spec)

    assert len(papers) == 1
    paper = papers[0]
    assert paper["pmid"] == "111"
    assert paper["title"] == "A trial of X"
    assert paper["authors"] == ["Smith J", "Doe A"]
    assert paper["journal"] == "NEJM"
    assert paper["abstract"] == "Background... Results..."
    assert paper["source_specialty"] == "cardiology"
    assert paper["url"] == "https://pubmed.ncbi.nlm.nih.gov/111/"


async def test_search_and_fetch_handles_no_results(monkeypatch, query_spec):
    monkeypatch.setattr(mcp_tool_runner, "query", _fake_query_factory({"pmids": []}, {"articles": []}))

    async with PubMedMCPClient() as client:
        papers = await client.search_and_fetch(query_spec)

    assert papers == []


async def test_search_and_fetch_never_raises_on_tool_error(monkeypatch, query_spec):
    async def _erroring_query(*, prompt, options):
        yield AssistantMessage(
            content=[ToolUseBlock(id="call_1", name=PUBMED_TOOL_SEARCH, input={})],
            model="claude-haiku-4-5-20251001",
        )
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="call_1", content="boom", is_error=True)]
        )

    monkeypatch.setattr(mcp_tool_runner, "query", _erroring_query)

    async with PubMedMCPClient() as client:
        papers = await client.search_and_fetch(query_spec)

    assert papers == []


async def test_run_and_collect_tool_results_ignores_unmatched_tool_result(monkeypatch):
    async def _fake_query(*, prompt, options):
        yield UserMessage(
            content=[ToolResultBlock(tool_use_id="unknown_call", content=json.dumps({"x": 1}))]
        )

    monkeypatch.setattr(mcp_tool_runner, "query", _fake_query)

    from claude_agent_sdk import ClaudeAgentOptions

    results = await mcp_tool_runner.run_and_collect_tool_results("prompt", ClaudeAgentOptions())

    assert results == {}
