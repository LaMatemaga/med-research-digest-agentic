import os

# Model configuration for the med-research-digest pipeline.
# To find current model IDs: https://docs.anthropic.com/en/docs/about-claude/models

# --- Available options (as of May 2026) ---
# "claude-haiku-4-5-20251001"   — fastest and cheapest; ideal for high-volume structured tasks
# "claude-sonnet-4-6"           — balanced quality and speed; good default for most tasks
# "claude-opus-4-7"             — highest reasoning quality; best for nuanced synthesis

# High-volume steps: evidence grading, relevance scoring, and all voices.
# These run once per paper and produce short, structured outputs — speed matters more than depth.
MODEL_FAST = "claude-haiku-4-5-20251001"

# Final synthesis step only: one paragraph per paper blending all voice perspectives.
# This is the output the physician actually reads — quality matters most here.
MODEL_SMART = "claude-sonnet-4-6"

# --- Concurrency limits ---
# Every Claude Agent SDK call starts its own `claude` CLI (a Node.js process), and a call
# with mcp_servers also starts that server via npx (more Node processes). Firing one per
# paper at once exhausted memory on a Windows laptop at ~133 papers (WinError 1455).
# Each stage runs at most this many calls at a time. Set MAX_CONCURRENT_CLAUDE_CALLS in
# the environment (or .env) to tune without editing code; lower is slower but safer.
MAX_CONCURRENT_CLAUDE_CALLS = max(1, int(os.getenv("MAX_CONCURRENT_CLAUDE_CALLS", "4")))

MAX_CONCURRENT_DISCOVERY_CALLS = MAX_CONCURRENT_CLAUDE_CALLS  # + PubMed MCP server each
MAX_CONCURRENT_GRADING_CALLS = MAX_CONCURRENT_CLAUDE_CALLS  # + PubMed MCP server each
MAX_CONCURRENT_RELEVANCE_CALLS = MAX_CONCURRENT_CLAUDE_CALLS  # + PubMed MCP server each
MAX_CONCURRENT_TRIALS_CALLS = MAX_CONCURRENT_CLAUDE_CALLS  # + ClinicalTrials.gov MCP server each
MAX_CONCURRENT_VOICE_CALLS = MAX_CONCURRENT_CLAUDE_CALLS  # per paper; no MCP
MAX_CONCURRENT_SYNTHESIS_CALLS = MAX_CONCURRENT_CLAUDE_CALLS  # no MCP
