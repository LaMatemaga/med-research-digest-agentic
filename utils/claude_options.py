from claude_agent_sdk import ClaudeAgentOptions


def isolated_options(**kwargs) -> ClaudeAgentOptions:
    """ClaudeAgentOptions that load nothing from the machine the pipeline runs on.

    By default the CLI merges in the user's `~/.claude` settings, plugins and hooks
    (`setting_sources=None` loads every source) and any MCP servers from project
    `.mcp.json`, user/global config or plugins (`strict_mcp_config=False`). Here only the
    `mcp_servers` passed in (sent as `--mcp-config`) are loaded, and only the
    `tools`/`allowed_tools` passed in are available. Every Claude call in the pipeline
    goes through this.
    """
    return ClaudeAgentOptions(strict_mcp_config=True, setting_sources=[], **kwargs)


def text_only_options(system_prompt: str, model: str) -> ClaudeAgentOptions:
    """Options for a single-shot, text-generation-only Claude call (voices, synthesis).

    `tools=None` (the SDK default) sends no `--tools` flag, so the CLI would offer its full
    built-in tool set (WebSearch, WebFetch, Bash, ...). A tool call uses up the only turn
    and fails with "Reached maximum number of turns (1)", so all tools are removed here.
    """
    return isolated_options(
        system_prompt=system_prompt,
        model=model,
        max_turns=1,
        tools=[],
        allowed_tools=[],
        mcp_servers={},
    )
