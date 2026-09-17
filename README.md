# med-research-digest-agentic

**De un digest a un sistema agéntico de vigilancia clínica.**

This started as a one-shot script: run it, get a markdown newsletter, done. This repo
evolves that into an *agentic clinical-surveillance system* — the same physician-profile
pipeline, but now the model calls real tools (PubMed, ClinicalTrials.gov) instead of
reading pre-fetched abstracts, it remembers what it already showed you, it escalates the
highest-severity findings straight to a physician's phone via Telegram, and it runs on a
schedule instead of only when you remember to invoke it.

> This is a fork-and-evolve demo built for a Claude Meetup Healthcare session, on top of
> the original one-shot [`med-research-digest`](https://github.com/LaMatemaga/med-research-digest)
> (kept untouched — this is a separate repository, not a branch of it).

**What you get:** the same markdown digest in `outputs/`, now with matching clinical
trials attached to each paper, deduplicated against previous runs, and a Telegram alert
for anything that clears the top severity tier.

---

## How it works

```
PubMed MCP server (npx, no OAuth)          ClinicalTrials.gov MCP server (npx, no OAuth)
       ↓                                                  ↑
  Stage 1 — Discovery                                     |
    Claude + MCP tool calls (pubmed_search_articles,       |
    pubmed_fetch_articles) build PaperRecord dicts          |
       ↓                                                  |
  Seen-items memory (SQLite) — drop anything already shown |
       ↓                                                  |
  Stage 2 — Evidence grader (agentic: max_turns=4)          |
    may call pubmed_fetch_fulltext / pubmed_find_related    |
       ↓                                                  |
  Stage 3 — Relevance filter (agentic: max_turns=4)         |
    may call pubmed_fetch_fulltext / pubmed_find_related    |
       ↓                                                  |
  Stage 3b — Clinical trials cross-check ───────────────────┘
    clinicaltrials_search_studies by condition/intervention
       ↓
  Stage 4 — Voices (parallel) — clinician / methodologist / specialist / researcher / educator
       ↓
  Stage 5 — Synthesizer — one cohesive paragraph per paper
       ↓
  Stage 6 — Formatter — outputs/YYYY-MM-DD.md (papers + matching trials)
       ↓
  Severity gate — 🔴-tier papers → Telegram alert (Bot API, plain httpx POST)
```

**What actually changed vs. the original one-shot digest:**

| # | Change | Where |
|---|---|---|
| 1 | Raw `httpx` E-utilities calls → real MCP tool calls (`@cyanheads/pubmed-mcp-server`) | `search/pubmed_mcp_client.py`, `search/mcp_config.py`, `search/mcp_tool_runner.py` (retired `search/xml_parser.py` and the old `search/pubmed_client.py`) |
| 2 | Evidence grading & relevance filtering are agentic (`max_turns=4`, model can call `pubmed_fetch_fulltext` / `pubmed_find_related` itself) | `pipeline/stage2_evidence_grader.py`, `pipeline/stage3_relevance_filter.py` |
| 3 | New clinical trials cross-check stage (`clinicaltrialsgov-mcp-server`) | `pipeline/stage3b_trials_crosscheck.py`, `search/clinicaltrials_mcp_client.py` |
| 4 | Persistent memory across runs (SQLite `seen_items`), `--reset-seen` flag | `storage/seen_store.py`, `main.py` |
| 5 | Severity-gated Telegram alerting for the top tier | `alerts/telegram.py`, `main.py` |
| 6 | Daily scheduled run + CI, via GitHub Actions | `.github/workflows/surveillance.yml`, `.github/workflows/ci.yml` |

---

## Requirements

- Python 3.10 or later
- **Node.js 18+** — the PubMed and ClinicalTrials.gov MCP servers are launched on demand
  via `npx`, and the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python)
  itself shells out to the `claude` CLI (`npm install -g @anthropic-ai/claude-code`)
- An [Anthropic API key](https://console.anthropic.com) (Claude powers all analysis agents)
- A [Telegram bot](#telegram-bot-setup-botfather) if you want alert delivery (optional —
  the pipeline runs fine without it, it just skips alerting)
- Internet access (PubMed and ClinicalTrials.gov are free, no account needed)

No OAuth setup is required for either MCP server — both are self-hosted, stdio-transport
servers spawned by the Claude Agent SDK's `mcp_servers` config, not Anthropic's hosted
`pubmed.mcp.claude.com` connector (which is OAuth-gated and not reachable the same way
from a headless script).

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/LaMatemaga/med-research-digest-agentic.git
cd med-research-digest-agentic
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv venv
```

Activate it:

- **Mac / Linux:** `source venv/bin/activate`
- **Windows:** `venv\Scripts\activate`

Then install:

```bash
pip install -r requirements.txt
# for running the test suite too:
pip install -r requirements-dev.txt
```

### 3. Install the Claude Code CLI and Node.js

The Claude Agent SDK drives everything through the `claude` CLI, and that CLI is what
launches the MCP servers via `npx`. Both need Node.js 18+ on your PATH:

```bash
npm install -g @anthropic-ai/claude-code
```

You don't need to install the PubMed or ClinicalTrials.gov MCP servers yourself —
`npx -y @cyanheads/pubmed-mcp-server@latest` and `npx -y clinicaltrialsgov-mcp-server@latest`
download and run them on first use.

### 4. Set up your API keys and bot token

Copy the example file:

```bash
cp .env.example .env
```

Open `.env` and fill in:

```
ANTHROPIC_API_KEY=sk-ant-api03-your-key-here
NCBI_API_KEY=                          # optional, see below
TELEGRAM_BOT_TOKEN=your-bot-token      # optional, see Telegram setup below
TELEGRAM_CHAT_ID=your-chat-id          # optional
```

The `NCBI_API_KEY` line is optional. Leave it blank for standard PubMed access (3 requests/second), or add a free NCBI key for higher limits (10/second). Get one at [account.ncbi.nlm.nih.gov/settings](https://account.ncbi.nlm.nih.gov/settings/).

`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are optional too — without them, the pipeline
runs exactly as before and just logs that alerting is unconfigured instead of sending
anything.

### Telegram bot setup (BotFather)

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot`, give it a name and a username ending in `bot`. BotFather replies with
   a token that looks like `123456789:ABCDEF...` — put that in `TELEGRAM_BOT_TOKEN`.
3. Send your new bot any message (e.g. "hi") so it has a chat to reply into.
4. In a browser, open `https://api.telegram.org/bot<your-token>/getUpdates` and look for
   `"chat":{"id": ...}` in the response — that number is your `TELEGRAM_CHAT_ID`.
   (For a group chat, add the bot to the group first; group chat IDs are negative numbers.)
5. Run the pipeline once with a paper that should hit the 🔴 tier — you should get a
   plain-text message on Telegram within seconds of the run finishing.

---

## Configure your profile

Open `context/physician.py` and fill in your details. Every field is optional, but the more you fill in, the more relevant your digest will be.

```python
PROFILE = {
    "name": "Dr. Ana Torres",
    "specialties": ["cardiology", "internal medicine"],
    "subspecialties": ["heart failure", "electrophysiology"],
    "role": ["clinician", "researcher"],     # see options below
    "practice_setting": "academic",          # see options below
    "country": "Mexico",
    "research_interests": ["SGLT2 inhibitors", "cardiac resynchronization"],
    "credentials": ["MD", "PhD", "FACC"],
    "patient_population": "adult",           # "adult", "pediatric", or "geriatric"
    "newsletter_preferences": {
        "max_papers": 10,                    # maximum papers per digest
        "min_evidence_level": "pilot",       # minimum study type to include
        "include_preprints": False,          # include bioRxiv / medRxiv?
        "language": "english",
    },
}
```

**`role` options** (you can have more than one):

| Value | Effect |
|---|---|
| `"clinician"` | Prioritizes RCTs, systematic reviews, guidelines |
| `"researcher"` | Includes broader article types; activates researcher voice |
| `"educator"` | Emphasizes reviews and guidelines; activates educator voice |
| `"resident"` | Includes case reports and clinical trials |
| `"fellow"` | Emphasizes RCTs and meta-analyses |

**`practice_setting` options:** `"academic"`, `"private"`, `"public_hospital"`, `"community"`

**Supported specialties and subspecialties** (use these exact strings in your profile):

```
Specialties:       cardiology, internal medicine, oncology, neurology, psychiatry,
                   pediatrics, surgery, emergency medicine, infectious disease,
                   rheumatology, endocrinology, gastroenterology, pulmonology,
                   nephrology, hematology, dermatology, ophthalmology, orthopedics,
                   geriatrics, obstetrics, gynecology, urology, radiology, anesthesiology

Subspecialties:    heart failure, electrophysiology, interventional cardiology,
                   neuro-oncology, child psychiatry, neonatology
```

Any specialty not in this list will be searched by free text in PubMed titles and abstracts.

---

## Run it

Make sure your virtual environment is active, then run from the project root.

### Basic usage

```bash
# Digest for the last 7 days (default)
python main.py

# Digest for the last 30 days
python main.py --days 30

# Focus on a single specialty, ignoring your profile specialties
python main.py --specialty oncology

# Preview what PubMed queries would run — no API calls made
python main.py --dry-run

# Clear remembered items so the next run surfaces everything again (demo control)
python main.py --reset-seen

# Log the raw input and raw, unparsed result of every MCP tool call to stderr
python main.py --days 30 --debug-mcp 2> mcp-debug.log
```

### Recommended first run

Start with `--dry-run` to confirm your profile produces the queries you expect:

```bash
python main.py --dry-run
```

Then do a wider window to make sure papers are found:

```bash
python main.py --days 30
```

### Demoing the memory feature live

Run the pipeline once normally, then run it again with the same `--days` window: the
second run's console output will show `0 new` for everything it already surfaced, because
`storage/seen_store.py` remembers every PMID it has shown you (in `data/seen_items.db`).
Use `--reset-seen` right before a demo run if you want the full result set to show up
again.

---

## Output

Each run saves a file to `outputs/YYYY-MM-DD.md`. Open it in any markdown viewer (VS Code, Obsidian, Typora, or paste into Notion).

Example structure:

```
# Medical Research Digest
2026-05-14 to 2026-05-21 | Dr. Ana Torres | cardiology, internal medicine | clinician, researcher

## Summary
| Tier | Papers |
| 🔴 High relevance, Level A evidence       | 2 |
| 🟡 Moderate relevance, Level A-B evidence | 4 |
| 🔵 Worth watching, Level C / early stage  | 3 |

---

## 🔴 High Relevance | Evidence Level A

🔴 **Dapagliflozin in Heart Failure with Mildly Reduced Ejection Fraction**
*McMurray JJ, Solomon SD, Inzucchi SE et al. — N Engl J Med (2026-May-15)*
[A Evidence | RCT] ⚠ industry funding

This multi-center RCT of 6,263 patients with HFmrEF demonstrates that dapagliflozin
reduces the composite of worsening heart failure or cardiovascular death by 18%...

[PubMed →](https://pubmed.ncbi.nlm.nih.gov/...)

**Related registered trials:**
- [NCT04008394: DELIVER — Dapagliflozin in HFmrEF/HFpEF](https://clinicaltrials.gov/study/NCT04008394) (Completed | Phase 3)
```

Papers that clear stage 3's relevance filter get cross-checked against
ClinicalTrials.gov (stage 3b); when a matching trial exists by condition/intervention, it
shows up right under the paper, both in the markdown digest and in the Telegram alert.

**Evidence tiers:**
- 🔴 High relevance + Level A (meta-analysis or large RCT)
- 🟡 Moderate relevance + Level A or B
- 🔵 Worth watching (early stage, case series, or lower relevance score)

**Methodology flags** appear when a paper has concerns worth noting: `small sample`, `no control group`, `industry funding`, `single center`, `surrogate outcome`, etc.

---

## Console summary

After each run you'll see:

```
==================================================
med-research-digest complete
  Papers found:    47
  Papers included: 8
  🔴 High:         2
  🟡 Moderate:     4
  🔵 Watching:     2
  Telegram alerts: 2
  Saved to:        outputs/2026-05-21.md
==================================================
```

---

## Testing

The test suite mocks every external call — no network access, no real API keys, no
`npx`/MCP subprocess, no `claude` CLI — so it runs anywhere:

```bash
pip install -r requirements-dev.txt
pytest -v
```

It covers:
- **Seen-store dedup** (`tests/test_seen_store.py`) — the SQLite memory logic, using a
  real temp SQLite file (no mocking needed, just isolation via `tmp_path`).
- **PubMed MCP client wrapper** (`tests/test_pubmed_mcp_client.py`) — feeds a fake
  `claude_agent_sdk.query()` that yields synthetic `ToolUseBlock`/`ToolResultBlock`
  pairs, and checks the wrapper reads the *raw* tool results correctly.
- **Telegram alert function** (`tests/test_telegram_alert.py`) — mocks `httpx.AsyncClient`
  and checks the plain-text formatting and POST payload.
- **Full pipeline smoke test** (`tests/test_pipeline_smoke.py`) — runs all seven stages
  end to end with every external call mocked, proving the whole thing doesn't crash
  without network or real credentials, and that the seen-items memory actually
  suppresses a paper on a second run.

## Continuous integration & scheduling

- `.github/workflows/ci.yml` — runs `pytest` on every push and pull request.
- `.github/workflows/surveillance.yml` — runs the pipeline daily (`cron: "0 7 * * *"`) and
  on demand (`workflow_dispatch`), using repository secrets (`ANTHROPIC_API_KEY`,
  `NCBI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — configure these in the repo's
  Settings → Secrets, this repo does not create them for you). It installs Node.js and the
  `claude` CLI so the MCP servers can launch, and caches `data/seen_items.db` between runs
  so the daily job's memory persists.

---

## Future work (explicitly out of scope for this demo)

- **openFDA** integration (adverse event / label data cross-referencing).
- **RxNorm / MeSH normalization services** for drug-name and term canonicalization.
- Wiring in Anthropic's **official OAuth-gated MCP connectors**
  (e.g. `pubmed.mcp.claude.com`) once a persistent, browser-authenticated deployment
  target (vs. this headless script) makes that flow practical.
- **Sharing MCP server processes across calls.** Today every MCP-enabled call starts its
  own `claude` CLI and its own `npx` MCP server, and the concurrency cap is what keeps
  that affordable. Two ways to share them, neither verified live yet: run each MCP server
  once per pipeline run over Streamable HTTP (both servers support it) and point calls at
  it with an `http` MCP config, or keep one long-lived `ClaudeSDKClient` per stage (this
  one needs care so one paper's conversation doesn't leak into the next).

---

## Model configuration

Open `config.py` to control which Claude model each stage uses:

```python
# High-volume steps: evidence grading, relevance scoring, all voices
MODEL_FAST = "claude-haiku-4-5-20251001"

# Final synthesis only: the paragraph the physician actually reads
MODEL_SMART = "claude-sonnet-4-6"
```

The pipeline is split so cheaper, faster models handle the structured classification work (grading, scoring, short voice paragraphs), while the stronger model is reserved for the one output that matters most — the synthesized narrative per paper.

To find current model IDs: [docs.anthropic.com/en/docs/about-claude/models](https://docs.anthropic.com/en/docs/about-claude/models)

## Concurrency (memory safety)

Every Claude call starts a `claude` CLI process (Node.js). Calls in the discovery, grading,
relevance and trials stages also start an MCP server through `npx`, which adds more Node
processes. Each stage therefore runs at most **`MAX_CONCURRENT_CLAUDE_CALLS`** calls at a time
(default **4**). Set it in `.env` or the environment:

```
MAX_CONCURRENT_CLAUDE_CALLS=2   # low-memory laptop / live demo
MAX_CONCURRENT_CLAUDE_CALLS=8   # workstation or CI runner with plenty of RAM
```

You can also tune each stage separately in `config.py` (`MAX_CONCURRENT_GRADING_CALLS`,
`MAX_CONCURRENT_RELEVANCE_CALLS`, `MAX_CONCURRENT_TRIALS_CALLS`, …).

A lower cap is safer but slower. Grading and relevance each make one call per new paper,
so a 133-paper run is ~266 calls through those two stages alone. For a live demo, keep the
input small: `--days 1`, a single `--specialty`, or a first run followed by a re-run
(the seen-items memory skips papers it has already processed).

---

## Troubleshooting

**`WinError 1455` / "El archivo de paginación es demasiado pequeño" / exit code 3221226505 / "Control request timeout: initialize"**
The machine ran out of memory starting Claude CLI and MCP server processes. Lower
`MAX_CONCURRENT_CLAUDE_CALLS` (try `2`) and use a smaller run (`--days 1` or `--specialty`).

**"No specialties configured"**
Fill in `specialties` in `context/physician.py`, or use `--specialty cardiology` on the command line.

**"All papers filtered out"**
Try `--days 30` for a wider window, or `--specialty` to focus on one area. The relevance filter is strict when your profile is sparse — add more details (subspecialties, research interests) to improve scoring.

**Papers found but no 🔴 entries**
The 🔴 tier requires both relevance score ≥ 8 AND evidence level A. This is intentional — high-tier entries represent truly practice-relevant meta-analyses or large RCTs.

**Rate limit warning from PubMed**
Add an `NCBI_API_KEY` to your `.env` file (free registration). This raises the limit from 3 to 10 requests per second.

**`ModuleNotFoundError`**
Make sure your virtual environment is active (`source venv/bin/activate` or `venv\Scripts\activate`) before running.

**`CLINotFoundError` or the run hangs on Stage 1**
The Claude Agent SDK needs the `claude` CLI on your PATH (`npm install -g @anthropic-ai/claude-code`), and that CLI needs Node.js 18+ to launch the PubMed/ClinicalTrials.gov MCP servers via `npx`. Run `claude --version` and `npx --version` to confirm both are available before filing a bug.

**"search found N PMIDs but … yielded no parseable articles"**
Re-run with `--debug-mcp 2> mcp-debug.log`. Every MCP tool call's arguments and raw result (plus the CLI's raw `tool_use_result` envelope) get logged there, and any payload the parsers can't read is also printed as a warning with the raw text. `search/pubmed_mcp_client.py` handles both forms the PubMed server produces (`structuredContent` JSON and its markdown `content[]`) and fetches in batches of `FETCH_BATCH_SIZE` so large results don't hit the CLI's MCP output cap.

**No Telegram alert arrives**
Check that `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are both set in `.env`, that you've messaged your bot at least once (Telegram won't let a bot message a chat it hasn't seen), and that at least one paper actually reached the 🔴 tier this run (alerting is intentionally gated to the top severity tier only).

**Digest looks identical to last time / nothing new**
That's the seen-items memory (`storage/seen_store.py`) working as intended — re-runs only surface genuinely new papers. Use `--reset-seen` to see the full result set again.

---

## Privacy

This tool queries PubMed and ClinicalTrials.gov with your specialty terms only — your physician profile stays local and is never sent to either. The profile is sent to Claude (Anthropic API) as context for relevance scoring and synthesis. Telegram alerts are sent only for the top severity tier, to the chat ID you configure. Do not include patient data of any kind in the profile.

---

## Cost estimate

Each paper goes through approximately 9 Claude calls: 1 for evidence grading, 1 for relevance scoring, up to 6 voices (depending on your role configuration), and 1 for synthesis. The number of voices active depends on the `role` field in your profile — a clinician-only profile activates 3 voices (clinician, methodologist, gremial); adding researcher, educator, and specialties can reach 6–7.

**Measured reference** (110 papers processed, 5 active voices):

| Configuration | Cost |
|---|---|
| Default — `MODEL_FAST` = Haiku, `MODEL_SMART` = Sonnet | **~$4.82** |
| All Sonnet — both `MODEL_FAST` and `MODEL_SMART` = Sonnet | **~$7.60** |

**What can make your actual cost higher or lower:**

- **Number of papers.** Cost scales roughly linearly. 55 papers ≈ half the price; 220 papers ≈ double.
- **Number of active voices.** Each role you add to your profile activates more voices. A researcher + educator + 2 specialties profile runs ~7 voice calls per paper instead of 3.
- **Abstract length.** Longer abstracts mean more input tokens per call. Review articles and meta-analyses tend to have longer abstracts than brief reports.
- **Prompt length changes.** If you customize the system prompts in any of the voice or pipeline files, token counts will shift accordingly.
- **Model pricing changes.** Anthropic adjusts pricing over time. Check current rates at [anthropic.com/pricing](https://www.anthropic.com/pricing).
- **Lookback window.** A `--days 30` run will pull more papers from PubMed, most of which get filtered out before reaching Claude — but papers that pass the relevance filter all incur the full voice + synthesis cost.
- **Agentic tool calls.** Stages 2 and 3 now run with `max_turns=4` and MCP tool access instead of `max_turns=1`. Most papers still resolve in a single turn (the abstract is enough), but a paper the model decides needs full text or related-article context costs a few extra turns — and the seen-items memory (item 4) means this cost is only paid once per paper, ever, not on every re-run.
