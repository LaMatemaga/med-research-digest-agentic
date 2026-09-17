"""Tool-using stages must load only this repo's MCP servers and tools: nothing from the
presenter's ~/.claude settings, plugins, or project/user .mcp.json. They must also keep
their real tool access. Each check fails if either side regresses."""

import dataclasses
import json
import pathlib
import re

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

import pipeline.stage2_evidence_grader as stage2
import pipeline.stage3_relevance_filter as stage3
from search import mcp_tool_runner
from search.clinicaltrials_mcp_client import find_matching_trials
from search.mcp_config import (
    CLINICALTRIALS_TOOL_SEARCH,
    PUBMED_TOOL_FETCH,
    PUBMED_TOOL_FETCH_FULLTEXT,
    PUBMED_TOOL_FIND_RELATED,
    PUBMED_TOOL_SEARCH,
)
from search.pubmed_mcp_client import PubMedMCPClient

PAPER = {"pmid": "1", "title": "T", "abstract": "A", "mesh_terms": ["Heart Failure"]}
PUBMED_PACKAGE = "@cyanheads/pubmed-mcp-server@latest"
TRIALS_PACKAGE = "clinicaltrialsgov-mcp-server@latest"


def _capturing_query(captured: list, text: str = "{}"):
    async def _query(*, prompt, options):
        captured.append(options)
        yield AssistantMessage(content=[TextBlock(text=text)], model="m")

    return _query


async def _discovery_options(monkeypatch):
    captured: list = []
    monkeypatch.setattr(mcp_tool_runner, "query", _capturing_query(captured))
    async with PubMedMCPClient() as client:
        await client.search_and_fetch({"specialty": "cardiology", "query": "q", "retmax": 30})
    return captured


async def _grading_options(monkeypatch):
    captured: list = []
    monkeypatch.setattr(stage2, "query", _capturing_query(captured))
    await stage2.grade_paper(PAPER)
    return captured


async def _relevance_options(monkeypatch):
    captured: list = []
    monkeypatch.setattr(stage3, "query", _capturing_query(captured))
    await stage3.score_paper(PAPER, "system")
    return captured


async def _trials_options(monkeypatch):
    captured: list = []
    monkeypatch.setattr(mcp_tool_runner, "query", _capturing_query(captured))
    await find_matching_trials(PAPER)
    return captured


STAGES = [
    ("discovery", _discovery_options, "pubmed", PUBMED_PACKAGE, {PUBMED_TOOL_SEARCH, PUBMED_TOOL_FETCH}),
    ("grading", _grading_options, "pubmed", PUBMED_PACKAGE, {PUBMED_TOOL_FETCH_FULLTEXT, PUBMED_TOOL_FIND_RELATED}),
    ("relevance", _relevance_options, "pubmed", PUBMED_PACKAGE, {PUBMED_TOOL_FETCH_FULLTEXT, PUBMED_TOOL_FIND_RELATED}),
    ("trials", _trials_options, "clinicaltrials", TRIALS_PACKAGE, {CLINICALTRIALS_TOOL_SEARCH}),
]


@pytest.mark.parametrize("name,get_options,server,package,tools", STAGES, ids=[s[0] for s in STAGES])
async def test_tool_stage_is_isolated_but_keeps_its_tools(monkeypatch, name, get_options, server, package, tools):
    [options] = await get_options(monkeypatch)

    # Isolation: no ambient settings, plugins, or MCP config, and no built-in tools
    # (WebSearch/WebFetch/Bash...) alongside the MCP tools.
    assert options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.tools == []

    # Real tool access is intact: exactly this repo's server, launched via npx, and the
    # stage's tools pre-approved.
    assert set(options.mcp_servers) == {server}
    assert options.mcp_servers[server]["command"] == "npx"
    assert package in options.mcp_servers[server]["args"]
    assert set(options.allowed_tools) == tools

    # And the SDK turns that into the CLI flags we expect.
    # Both halves must be in the same CLI command: built-ins off (`--tools ""`) AND this
    # stage's MCP server + pre-approved MCP tools still passed.
    cmd = SubprocessCLITransport(prompt="p", options=dataclasses.replace(options, cli_path="claude"))._build_command()
    assert "--strict-mcp-config" in cmd
    assert "--setting-sources=" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    mcp_config = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert set(mcp_config["mcpServers"]) == {server}
    assert set(cmd[cmd.index("--allowedTools") + 1].split(",")) == tools


def test_no_call_site_builds_claude_agent_options_directly():
    """Every Claude call must go through utils/claude_options, so a new stage can't
    quietly bring back ambient config loading."""
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = [
        str(path.relative_to(root))
        for folder in ("pipeline", "search", "voices", "alerts", "storage")
        for path in (root / folder).rglob("*.py")
        if re.search(r"\bClaudeAgentOptions\(", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
