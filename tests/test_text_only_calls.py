"""Voices and synthesis are single-turn text generation. With the SDK defaults the CLI
still offers built-in tools and the user's own MCP servers/settings; one tool call uses
the only turn ("Reached maximum number of turns (1)") and that voice is silently lost.
"""

import dataclasses

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

import pipeline.stage5_synthesizer as stage5
from utils.claude_options import text_only_options
from voices import clinician, educator, gremial, methodologist, researcher, specialist

PAPER = {"pmid": "1", "title": "T", "abstract": "A", "voice_outputs": {"clinician_voice": "c"}}


def _assert_text_only(options):
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.setting_sources == []
    assert options.max_turns == 1


def _capturing_query(captured: list):
    async def _query(*, prompt, options):
        captured.append(options)
        yield AssistantMessage(content=[TextBlock(text="ok")], model="m")

    return _query


VOICE_CALLS = [
    (clinician, clinician.analyze),
    (educator, educator.analyze),
    (gremial, gremial.analyze),
    (methodologist, methodologist.analyze),
    (researcher, researcher.analyze),
    (specialist, specialist.make_specialist_voice("cardiology")),
]


@pytest.mark.parametrize("module,analyze", VOICE_CALLS, ids=lambda v: getattr(v, "__name__", ""))
async def test_every_voice_call_has_no_tool_access(monkeypatch, module, analyze):
    captured: list = []
    monkeypatch.setattr(module, "query", _capturing_query(captured))

    assert await analyze(PAPER) == "ok"

    [options] = captured
    _assert_text_only(options)


async def test_synthesizer_call_has_no_tool_access(monkeypatch):
    captured: list = []
    monkeypatch.setattr(stage5, "query", _capturing_query(captured))

    await stage5.synthesize_paper(PAPER)

    [options] = captured
    _assert_text_only(options)


def test_text_only_options_reach_the_cli_as_no_tools_and_isolated_config():
    """Checks the real SDK translation of these options into `claude` CLI flags."""
    options = dataclasses.replace(text_only_options("sys", "claude-haiku-4-5-20251001"), cli_path="claude")
    cmd = SubprocessCLITransport(prompt="p", options=options)._build_command()

    tools_flag = cmd.index("--tools")
    assert cmd[tools_flag + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert "--setting-sources=" in cmd
    assert "--mcp-config" not in cmd
    assert "--allowedTools" not in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "1"


def test_sdk_defaults_are_not_tool_free():
    """Documents the root cause: bare ClaudeAgentOptions leaves the CLI's default
    tools and the user's MCP/settings sources switched on."""
    from claude_agent_sdk import ClaudeAgentOptions

    cmd = SubprocessCLITransport(
        prompt="p", options=ClaudeAgentOptions(system_prompt="s", max_turns=1, cli_path="claude")
    )._build_command()

    assert "--tools" not in cmd
    assert "--strict-mcp-config" not in cmd
    assert not any(part.startswith("--setting-sources") for part in cmd)
