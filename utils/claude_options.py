from claude_agent_sdk import ClaudeAgentOptions


def text_only_options(system_prompt: str, model: str) -> ClaudeAgentOptions:
    """Options for a single-shot, text-generation-only Claude call (voices, synthesis).

    The SDK's defaults are not tool-free: `tools=None` sends no `--tools` flag, so the CLI
    offers its full built-in tool set (WebSearch, WebFetch, Bash, ...), and
    `strict_mcp_config=False` / `setting_sources=None` let it load the user's own settings,
    plugins and MCP servers. A tool call uses up the only turn and fails with "Reached
    maximum number of turns (1)". These options remove every tool source, so the one turn
    can only be text.
    """
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        max_turns=1,
        tools=[],
        allowed_tools=[],
        mcp_servers={},
        strict_mcp_config=True,
        setting_sources=[],
    )
