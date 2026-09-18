from claude_agent_sdk import ClaudeAgentOptions


def isolated_options(**kwargs) -> ClaudeAgentOptions:
    """ClaudeAgentOptions that load nothing from the machine the pipeline runs on and
    offer the model no built-in tools. Every Claude call in the pipeline goes through this.

    - `strict_mcp_config=True`, `setting_sources=[]`: by default the CLI merges in the
      user's `~/.claude` settings, plugins and hooks, and MCP servers from `.mcp.json`,
      user/global config or plugins. Here only the `mcp_servers` passed in load.
    - `tools=[]` (`--tools ""`): by default the CLI offers its built-in tools (WebSearch,
      WebFetch, Bash, ...). This removes only those: the bundled CLI turns `--tools ""`
      into deny rules for its built-in tool names (getAllBaseTools), and builds the
      model's tool list as built-ins plus MCP tools, each filtered by those rules. The
      `mcp_servers` tools this repo passes stay available, pre-approved by `allowed_tools`.
    """
    return ClaudeAgentOptions(strict_mcp_config=True, setting_sources=[], tools=[], **kwargs)


def text_only_options(system_prompt: str, model: str) -> ClaudeAgentOptions:
    """Options for a single-shot, text-generation-only Claude call (voices, synthesis):
    isolated, no built-in tools, and no MCP servers either. Any tool call would use up the
    only turn and fail with "Reached maximum number of turns (1)"."""
    return isolated_options(
        system_prompt=system_prompt,
        model=model,
        max_turns=1,
        allowed_tools=[],
        mcp_servers={},
    )
