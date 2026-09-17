"""Severity-gated alerting: POSTs a concise, physician-readable message to Telegram
for the top-tier (🔴 high relevance + strong evidence) papers in a run. Uses the
existing httpx dependency directly against the Bot API — no extra Telegram library."""

import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
_MAX_SUMMARY_CHARS = 500


def _strip_markdown(text: str) -> str:
    """Converts the digest's markdown synthesis into plain, physician-readable text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*]\s+", "- ", text, flags=re.MULTILINE)
    return text.strip()


def format_alert_message(paper: dict) -> str:
    """Builds a concise, plain-text alert for one high-tier paper — no raw markdown."""
    title = paper.get("title", "Untitled")
    journal = paper.get("journal", "")
    pub_date = paper.get("pub_date", "")
    evidence_level = paper.get("evidence_level", "C")
    study_design = paper.get("study_design", "unknown")
    score = paper.get("relevance_score", "")
    synthesis = paper.get("synthesis", paper.get("abstract", ""))
    url = paper.get("url", "")

    summary = _strip_markdown(synthesis)
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[: _MAX_SUMMARY_CHARS - 3].rstrip() + "..."

    trials = paper.get("matching_trials") or []
    trial_ids = [t.get("nct_id", "") for t in trials if t.get("nct_id")]
    trial_line = f"Matching trials: {', '.join(trial_ids)}" if trial_ids else ""

    lines = [
        f"HIGH-RELEVANCE ALERT (score {score}/10, Level {evidence_level} evidence — {study_design})",
        title,
        f"{journal} ({pub_date})".strip(),
        "",
        summary,
        trial_line,
        "",
        url,
    ]
    return "\n".join(line for line in lines if line != "")


async def send_alert(
    paper: dict,
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """POSTs a physician-readable alert to Telegram. Never raises — returns False
    on any failure or missing configuration, True once Telegram accepts the message."""
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        logger.info("Telegram alerting not configured (missing bot token or chat id); skipping alert.")
        return False

    message = format_alert_message(paper)
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": message, "disable_web_page_preview": False}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        return True
    except httpx.HTTPError as e:
        logger.warning(f"Telegram alert failed for PMID {paper.get('pmid', '?')}: {e}")
        return False


async def send_alerts_for_tier(papers: list[dict], tier_fn, top_tier: str = "🔴") -> list[str]:
    """Sends alerts for every paper in the top tier. Returns the PMIDs successfully alerted."""
    alerted_pmids = []
    for paper in papers:
        if tier_fn(paper) != top_tier:
            continue
        if await send_alert(paper):
            alerted_pmids.append(paper.get("pmid", ""))
    return alerted_pmids
