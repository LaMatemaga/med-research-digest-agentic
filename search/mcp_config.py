"""Shared MCP stdio server configs for PubMed and ClinicalTrials.gov tool access.

Both servers are self-hosted, no-OAuth MCP servers launched on demand via `npx`
(no separate install step, no Anthropic-hosted connector). They are wired into
`ClaudeAgentOptions.mcp_servers` by the pipeline stages that need them.
"""

import os

from claude_agent_sdk.types import McpStdioServerConfig

PUBMED_SERVER_NAME = "pubmed"
CLINICALTRIALS_SERVER_NAME = "clinicaltrials"


def pubmed_mcp_server() -> McpStdioServerConfig:
    """@cyanheads/pubmed-mcp-server, stdio transport. NCBI_API_KEY is optional
    (raises the PubMed rate limit) and is forwarded from the environment if set."""
    env = {"MCP_TRANSPORT_TYPE": "stdio"}
    api_key = os.getenv("NCBI_API_KEY")
    if api_key:
        env["NCBI_API_KEY"] = api_key
    return {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@cyanheads/pubmed-mcp-server@latest"],
        "env": env,
    }


def clinicaltrials_mcp_server() -> McpStdioServerConfig:
    """clinicaltrialsgov-mcp-server, stdio transport. No API key required."""
    return {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "clinicaltrialsgov-mcp-server@latest"],
        "env": {"MCP_TRANSPORT_TYPE": "stdio"},
    }


# Fully-qualified tool names as the Claude Agent SDK exposes them: mcp__<server>__<tool>
PUBMED_TOOL_SEARCH = f"mcp__{PUBMED_SERVER_NAME}__pubmed_search_articles"
PUBMED_TOOL_FETCH = f"mcp__{PUBMED_SERVER_NAME}__pubmed_fetch_articles"
PUBMED_TOOL_FETCH_FULLTEXT = f"mcp__{PUBMED_SERVER_NAME}__pubmed_fetch_fulltext"
PUBMED_TOOL_FIND_RELATED = f"mcp__{PUBMED_SERVER_NAME}__pubmed_find_related"

CLINICALTRIALS_TOOL_SEARCH = f"mcp__{CLINICALTRIALS_SERVER_NAME}__clinicaltrials_search_studies"
CLINICALTRIALS_TOOL_GET_STUDY = f"mcp__{CLINICALTRIALS_SERVER_NAME}__clinicaltrials_get_study_record"
