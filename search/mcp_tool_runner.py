"""Generic driver for a single Claude Agent SDK turn whose job is to call MCP tools.

Instead of trusting the model to paraphrase tool output back into JSON prose (fragile),
this reads the *raw tool results* straight out of the SDK message stream — the
ToolUseBlock/ToolResultBlock pairs that the SDK yields when it executes an MCP tool call
on the model's behalf. That keeps downstream parsing deterministic even though a real
agent, with real turns, is choosing when and whether to call which tool.
"""

import json
import logging

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

logger = logging.getLogger(__name__)


async def run_and_collect_tool_results(prompt: str, options: ClaudeAgentOptions) -> dict[str, object]:
    """Runs one agent turn and returns {fully_qualified_tool_name: parsed_json_result}.

    Only successful (non-error) tool results are included. If a tool is called more than
    once in the turn, the last successful result wins.
    """
    pending_tool_names: dict[str, str] = {}
    results: dict[str, object] = {}

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        pending_tool_names[block.id] = block.name
            elif isinstance(msg, UserMessage):
                blocks = msg.content if isinstance(msg.content, list) else []
                for block in blocks:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    tool_name = pending_tool_names.get(block.tool_use_id)
                    if not tool_name or block.is_error:
                        continue
                    parsed = extract_tool_json(block.content)
                    if parsed is not None:
                        results[tool_name] = parsed
    except Exception as e:
        logger.warning(f"MCP tool-calling turn failed: {e}")

    return results


def extract_tool_json(content) -> object | None:
    """Parses an MCP tool result's content (str, or list of MCP content blocks) as JSON."""
    if content is None:
        return None
    if isinstance(content, str):
        return _safe_json(content)
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and "json" in item:
                return item["json"]
        texts = [item["text"] for item in content if isinstance(item, dict) and "text" in item]
        if texts:
            return _safe_json("\n".join(texts))
    return None


def _safe_json(text: str) -> object | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
