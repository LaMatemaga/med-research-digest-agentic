"""Generic driver for a single Claude Agent SDK turn whose job is to call MCP tools.

Both MCP servers this project uses (built on @cyanheads/mcp-ts-core) answer every tool
call with two separate fields: `content[]`, a markdown rendering from the tool's own
`format()`, and `structuredContent`, the JSON matching the tool's output schema. The
Python SDK's `ToolResultBlock` models only a single `content` value (claude_agent_sdk
`_internal/message_parser.py` reads `block.get("content")` and nothing else), and which
of the two the Claude Code CLI puts there — or whether it swaps an oversized result for
a truncation notice (MAX_MCP_OUTPUT_TOKENS) — is CLI behavior not visible from Python.
So each result keeps both a best-effort JSON parse and the raw text, and callers parse
whichever form actually arrived. Run with --debug-mcp to log every raw payload.
"""

import json
import logging
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolCallResult:
    """What one MCP tool call actually gave us, in both forms we might need.

    `structured`: best-effort JSON — from the enclosing UserMessage's `tool_use_result`
    dict if it carries a `structuredContent`/`structured_content` key, else from trying
    to JSON-parse the text content directly. `None` when neither worked, which is the
    expected outcome for the two real servers this project talks to (their tools only
    ever return markdown in `content[]` — see the module docstring).

    `text`: the raw text content, joined if there were multiple text blocks. Always
    populated when the tool call succeeded, since content[] is what's guaranteed.
    """

    structured: object | None = None
    text: str = ""
    is_error: bool = False


async def run_and_collect_tool_results(
    prompt: str, options: ClaudeAgentOptions
) -> dict[str, list[ToolCallResult]]:
    """Runs one agent turn and returns {fully_qualified_tool_name: [ToolCallResult, ...]}
    in call order. Every call is kept — a tool called in batches (e.g. fetching PMIDs a
    few at a time) must not have earlier batches overwritten by later ones.
    """
    pending_tool_names: dict[str, str] = {}
    results: dict[str, list[ToolCallResult]] = {}

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        pending_tool_names[block.id] = block.name
                        logger.debug(f"MCP tool_use: {block.name} input={block.input!r}")
            elif isinstance(msg, UserMessage):
                blocks = msg.content if isinstance(msg.content, list) else []
                for block in blocks:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    tool_name = pending_tool_names.get(block.tool_use_id)
                    logger.debug(
                        f"MCP tool_result: tool={tool_name} is_error={block.is_error} "
                        f"content={block.content!r} tool_use_result={msg.tool_use_result!r}"
                    )
                    if not tool_name:
                        continue
                    result = _to_tool_call_result(block, msg.tool_use_result)
                    if result.is_error:
                        logger.warning(f"MCP tool {tool_name} returned an error: {result.text[:500]!r}")
                    results.setdefault(tool_name, []).append(result)
    except Exception as e:
        logger.warning(f"MCP tool-calling turn failed: {type(e).__name__}: {e}", exc_info=logger.isEnabledFor(logging.DEBUG))

    return results


def _to_tool_call_result(block: ToolResultBlock, tool_use_result: dict | None) -> ToolCallResult:
    text = _join_text_content(block.content)
    structured = _structured_from_tool_use_result(tool_use_result)
    if structured is None:
        structured = _safe_json(text) if text else None
    return ToolCallResult(structured=structured, text=text, is_error=bool(block.is_error))


def _join_text_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [item["text"] for item in content if isinstance(item, dict) and "text" in item]
        return "\n".join(texts)
    return ""


def _structured_from_tool_use_result(tool_use_result: dict | None) -> object | None:
    """Best-effort read of a raw structuredContent the CLI *might* pass through on the
    enclosing message's `tool_use_result` — undocumented, so this is a bonus path, not
    something callers should rely on being present (see module docstring)."""
    if not isinstance(tool_use_result, dict):
        return None
    for key in ("structuredContent", "structured_content"):
        value = tool_use_result.get(key)
        if value is not None:
            return value
    return None


def _safe_json(text: str) -> object | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
