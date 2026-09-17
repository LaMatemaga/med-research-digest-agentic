import json
import logging

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ToolResultBlock, ToolUseBlock, UserMessage

from search import mcp_tool_runner
from search.mcp_config import PUBMED_TOOL_FETCH, PUBMED_TOOL_SEARCH
from search.mcp_tool_runner import ToolCallResult
from search.pubmed_mcp_client import PubMedMCPClient, extract_articles, extract_pmids
from tests.fixtures import mcp_payloads as fx

MODEL = "claude-haiku-4-5-20251001"


def _text_blocks(text: str) -> list[dict]:
    """The MCP content[] envelope: a list of {type: text, text: ...} blocks."""
    return [{"type": "text", "text": text}]


def _fake_query(calls: list[tuple[str, object, dict | None]], captured: dict | None = None):
    """Fake claude_agent_sdk.query() yielding one ToolUseBlock/ToolResultBlock pair per
    (tool_name, content, tool_use_result) — no subprocess, no network."""

    async def _query(*, prompt, options):
        if captured is not None:
            captured["options"] = options
        for i, (tool_name, content, tool_use_result) in enumerate(calls):
            yield AssistantMessage(content=[ToolUseBlock(id=f"call_{i}", name=tool_name, input={})], model=MODEL)
            yield UserMessage(
                content=[ToolResultBlock(tool_use_id=f"call_{i}", content=content)],
                tool_use_result=tool_use_result,
            )

    return _query


@pytest.fixture
def query_spec():
    return {"specialty": "neurology", "query": "Nervous System Diseases[MeSH]", "retmax": 30}


# --- parsers against the real markdown format() output --------------------------------


def test_extract_pmids_from_real_search_markdown():
    assert extract_pmids(ToolCallResult(text=fx.SEARCH_MARKDOWN)) == ["40000001", "40000002", "40000003"]


def test_extract_pmids_no_results_is_empty_without_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert extract_pmids(ToolCallResult(text=fx.SEARCH_MARKDOWN_NO_RESULTS)) == []
    assert not caplog.records


def test_extract_articles_from_real_fetch_markdown():
    articles = extract_articles(ToolCallResult(text=fx.FETCH_MARKDOWN))

    assert [a["pmid"] for a in articles] == ["40000001", "40000002"]
    first = articles[0]
    assert first["title"] == "SGLT2 Inhibitors in Heart Failure: A [Randomized] Trial"
    assert first["authors"] == ["John A Smith", "Heart Failure Collaborative"]
    assert first["journal"] == "The New England journal of medicine"
    assert first["pub_date"] == "2026 Mar 5"
    assert first["publication_types"] == ["Journal Article", "Randomized Controlled Trial"]
    assert first["abstract"] == "BACKGROUND: SGLT2 inhibitors were tested. RESULTS: Mortality fell."
    assert first["mesh_terms"] == ["Heart Failure", "Humans"]
    assert first["pubmed_url"] == "https://pubmed.ncbi.nlm.nih.gov/40000001/"
    assert articles[1]["abstract"] == "A cohort of 400 patients."


def test_extract_articles_zero_returned_is_empty_without_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert extract_articles(ToolCallResult(text=fx.FETCH_MARKDOWN_EMPTY)) == []
    assert not caplog.records


# --- parsers against the real structuredContent schema --------------------------------


def test_extract_pmids_from_structured_content():
    assert extract_pmids(ToolCallResult(structured=fx.SEARCH_STRUCTURED)) == ["40000001", "40000002", "40000003"]


def test_extract_articles_from_structured_content():
    [article] = extract_articles(ToolCallResult(structured=fx.FETCH_STRUCTURED))

    assert article["pmid"] == "40000001"
    assert article["authors"] == ["Smith JA", "Heart Failure Collaborative"]
    assert article["journal"] == "The New England journal of medicine"
    assert article["pub_date"] == "2026-Mar-5"
    assert article["mesh_terms"] == ["Heart Failure", "Humans"]


# --- the regression: unparseable payloads must be loud, not a silent [] ---------------


def test_unparseable_fetch_payload_logs_raw_payload(caplog):
    with caplog.at_level(logging.WARNING):
        assert extract_articles(ToolCallResult(text=fx.CLI_TRUNCATION_NOTICE), "neurology") == []

    assert any(fx.CLI_TRUNCATION_NOTICE[:60] in r.getMessage() for r in caplog.records)


# --- end to end through the SDK message stream ----------------------------------------


async def _run_client(query_spec) -> list[dict]:
    async with PubMedMCPClient() as client:
        return await client.search_and_fetch(query_spec)


async def test_search_and_fetch_with_real_markdown_envelope(monkeypatch, query_spec):
    """The live failure: search and fetch both answer with content[] markdown. The old
    code JSON-parsed content[] and silently returned no articles."""
    monkeypatch.setattr(
        mcp_tool_runner,
        "query",
        _fake_query(
            [
                (PUBMED_TOOL_SEARCH, _text_blocks(fx.SEARCH_MARKDOWN), None),
                (PUBMED_TOOL_FETCH, _text_blocks(fx.FETCH_MARKDOWN), None),
            ]
        ),
    )

    papers = await _run_client(query_spec)

    assert [p["pmid"] for p in papers] == ["40000001", "40000002"]
    assert papers[0]["source_specialty"] == "neurology"
    assert papers[0]["url"] == "https://pubmed.ncbi.nlm.nih.gov/40000001/"


async def test_search_and_fetch_with_structured_content_as_json_text(monkeypatch, query_spec):
    monkeypatch.setattr(
        mcp_tool_runner,
        "query",
        _fake_query(
            [
                (PUBMED_TOOL_SEARCH, _text_blocks(json.dumps(fx.SEARCH_STRUCTURED)), None),
                (PUBMED_TOOL_FETCH, _text_blocks(json.dumps(fx.FETCH_STRUCTURED)), None),
            ]
        ),
    )

    papers = await _run_client(query_spec)

    assert [p["pmid"] for p in papers] == ["40000001"]


async def test_structured_content_on_tool_use_result_is_preferred(monkeypatch, query_spec):
    monkeypatch.setattr(
        mcp_tool_runner,
        "query",
        _fake_query(
            [
                (PUBMED_TOOL_SEARCH, _text_blocks(fx.SEARCH_MARKDOWN), {"structuredContent": fx.SEARCH_STRUCTURED}),
                (PUBMED_TOOL_FETCH, _text_blocks("unparseable"), {"structuredContent": fx.FETCH_STRUCTURED}),
            ]
        ),
    )

    papers = await _run_client(query_spec)

    assert [p["pmid"] for p in papers] == ["40000001"]


async def test_batched_fetch_calls_are_all_kept(monkeypatch, query_spec):
    """Fetches are batched; the old runner kept only the last call per tool."""
    batch_two = fx.FETCH_MARKDOWN.replace("40000001", "40000011").replace("40000002", "40000012")
    monkeypatch.setattr(
        mcp_tool_runner,
        "query",
        _fake_query(
            [
                (PUBMED_TOOL_SEARCH, _text_blocks(fx.SEARCH_MARKDOWN), None),
                (PUBMED_TOOL_FETCH, _text_blocks(fx.FETCH_MARKDOWN), None),
                (PUBMED_TOOL_FETCH, _text_blocks(batch_two), None),
            ]
        ),
    )

    papers = await _run_client(query_spec)

    assert [p["pmid"] for p in papers] == ["40000001", "40000002", "40000011", "40000012"]


async def test_options_request_pmids_batches_and_raise_cli_output_limit(monkeypatch, query_spec):
    captured: dict = {}
    monkeypatch.setattr(mcp_tool_runner, "query", _fake_query([], captured))

    await _run_client(query_spec)

    options = captured["options"]
    assert set(options.allowed_tools) == {PUBMED_TOOL_SEARCH, PUBMED_TOOL_FETCH}
    assert options.env["MAX_MCP_OUTPUT_TOKENS"] == "100000"
    assert "`pmids` array" in options.system_prompt
    assert options.max_turns >= 3 + 4  # 30 PMIDs -> 3 fetch batches, plus search + DONE headroom


async def test_fetch_error_result_is_logged_not_parsed(monkeypatch, query_spec, caplog):
    async def _query(*, prompt, options):
        yield AssistantMessage(content=[ToolUseBlock(id="s", name=PUBMED_TOOL_SEARCH, input={})], model=MODEL)
        yield UserMessage(content=[ToolResultBlock(tool_use_id="s", content=_text_blocks(fx.SEARCH_MARKDOWN))])
        yield AssistantMessage(content=[ToolUseBlock(id="f", name=PUBMED_TOOL_FETCH, input={})], model=MODEL)
        yield UserMessage(content=[ToolResultBlock(tool_use_id="f", content="NCBI 429", is_error=True)])

    monkeypatch.setattr(mcp_tool_runner, "query", _query)

    with caplog.at_level(logging.WARNING):
        papers = await _run_client(query_spec)

    assert papers == []
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "returned an error" in messages and "NCBI 429" in messages


async def test_debug_logging_emits_raw_input_and_payload(monkeypatch, caplog):
    monkeypatch.setattr(
        mcp_tool_runner,
        "query",
        _fake_query([(PUBMED_TOOL_FETCH, _text_blocks(fx.FETCH_MARKDOWN), {"raw": "envelope"})]),
    )

    with caplog.at_level(logging.DEBUG, logger="search.mcp_tool_runner"):
        await mcp_tool_runner.run_and_collect_tool_results("p", ClaudeAgentOptions())

    debug_text = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)
    assert "tool_use: " + PUBMED_TOOL_FETCH in debug_text
    assert "40000001" in debug_text  # the full raw content, unparsed
    assert "'raw': 'envelope'" in debug_text  # the raw tool_use_result too


async def test_unmatched_tool_result_is_ignored(monkeypatch):
    async def _query(*, prompt, options):
        yield UserMessage(content=[ToolResultBlock(tool_use_id="unknown", content=_text_blocks("x"))])

    monkeypatch.setattr(mcp_tool_runner, "query", _query)

    assert await mcp_tool_runner.run_and_collect_tool_results("p", ClaudeAgentOptions()) == {}
