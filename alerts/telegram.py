"""Severity-gated alerting for top-tier (🔴) papers, via the Telegram Bot API over httpx.

Each alert publishes the full analysis as a Telegraph page (alerts/telegraph.py) and sends
a short teaser linking to it. If Telegraph fails for any reason, the full analysis is sent
directly instead, formatted and trimmed to whole sentences, so an outage degrades the alert
rather than dropping it.

sendMessage facts used here, from https://core.telegram.org/bots/api#sendmessage:
- `text` is 1-4096 characters *after entities parsing* (i.e. visible text, tags removed).
  Entity offsets are UTF-16 code units, so length is measured that way here.
- parse_mode "HTML": only <b>, <i>, <a href>, … tags; `<`, `>`, `&` must be escaped.
- link previews are controlled by `link_preview_options` (`disable_web_page_preview` is
  no longer a sendMessage parameter).
"""

import html
import logging
import os
import re

import httpx

from alerts import telegraph
from alerts.text import split_sentences, strip_markdown, truncate_at_word

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4096
TEASER_LINK_LABEL = "📄 Leer análisis completo"
_MAX_TITLE_CHARS = 300
_MAX_HOOK_CHARS = 400
_TAG_RE = re.compile(r"<[^>]+>")


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _href(url: str) -> str:
    return html.escape(url, quote=True)


def visible_length(html_text: str) -> int:
    """Length Telegram counts against the 4096 limit: text after entities parsing, in
    UTF-16 code units."""
    visible = html.unescape(_TAG_RE.sub("", html_text))
    return len(visible.encode("utf-16-le")) // 2


def _synthesis_sentences(paper: dict) -> list[str]:
    return split_sentences(strip_markdown(paper.get("synthesis") or paper.get("abstract") or ""))


def _header_lines(paper: dict) -> list[str]:
    title = truncate_at_word(paper.get("title") or "Untitled", _MAX_TITLE_CHARS)
    grade = (
        f"Level {paper.get('evidence_level', 'C')} · {paper.get('study_design', 'unknown')} · "
        f"relevance {paper.get('relevance_score', '?')}/10"
    )
    return [f"🔴 <b>{_esc(title)}</b>", f"<i>{_esc(grade)}</i>"]


def format_teaser_message(paper: dict, page_url: str) -> str:
    """Short teaser: headline, tier/score, a one-sentence hook, link to the full page."""
    lines = _header_lines(paper)
    sentences = _synthesis_sentences(paper)
    if sentences and len(sentences[0]) <= _MAX_HOOK_CHARS:
        lines += ["", _esc(sentences[0])]
    lines += ["", f'<a href="{_href(page_url)}">{_esc(TEASER_LINK_LABEL)}</a>']
    return "\n".join(lines)


def format_alert_message(paper: dict, max_chars: int = TELEGRAM_MAX_CHARS) -> str:
    """Full alert sent directly when Telegraph is unavailable. Stays within max_chars of
    visible text by dropping whole sentences, never cutting one mid-way."""
    journal_line = " · ".join(p for p in (paper.get("journal", ""), paper.get("pub_date", "")) if p)
    head = _header_lines(paper)
    if journal_line:
        head.insert(1, f"<i>{_esc(journal_line)}</i>")

    tail: list[str] = []
    trials = [t for t in paper.get("matching_trials") or [] if t.get("nct_id")]
    if trials:
        tail += ["", "<b>Matching trials</b>"]
        for t in trials:
            link = f'<a href="{_href(t["url"])}">{_esc(t["nct_id"])}</a>' if t.get("url") else _esc(t["nct_id"])
            title = f" — {_esc(truncate_at_word(t['title'], 120))}" if t.get("title") else ""
            tail.append(f"• {link}{title}")
    if paper.get("url"):
        tail += ["", f'<a href="{_href(paper["url"])}">Read the abstract on PubMed</a>']

    sentences = _synthesis_sentences(paper)
    body: list[str] = []
    if sentences:
        body += ["", f"<b>Takeaway:</b> {_esc(sentences[-1])}", ""]
        points = sentences[:-1]
        included = []
        for i, sentence in enumerate(points):
            remaining = len(points) - i - 1
            note = [f"<i>(+{remaining} more in today's digest)</i>"] if remaining else []
            candidate = head + body + included + [f"• {_esc(sentence)}"] + note + tail
            if visible_length("\n".join(candidate)) > max_chars:
                omitted = len(points) - len(included)
                included.append(f"<i>(+{omitted} more in today's digest)</i>")
                break
            included.append(f"• {_esc(sentence)}")
        body += included

    message = "\n".join(head + body + tail)
    while visible_length(message) > max_chars and tail:
        tail.pop()
        message = "\n".join(head + body + tail)
    return message


async def _send_message(client: httpx.AsyncClient, token: str, chat_id: str, text: str) -> None:
    """sendMessage with parse_mode HTML. If Telegram can't parse the entities, resend the
    same content as plain text rather than failing the alert."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    resp = await client.post(url, json=payload)
    if resp.status_code == 400 and "parse entities" in resp.text:
        logger.warning(f"Telegram rejected the HTML ({resp.text[:200]}); resending as plain text")
        plain = {k: v for k, v in payload.items() if k != "parse_mode"}
        plain["text"] = html.unescape(_TAG_RE.sub("", text))
        resp = await client.post(url, json=plain)
    resp.raise_for_status()


async def send_alert(
    paper: dict,
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """Sends one alert. Never raises — returns False on missing configuration or if the
    Telegram message itself fails, True once Telegram accepts a message."""
    token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    pmid = paper.get("pmid", "?")
    if not token or not chat:
        logger.info("Telegram alerting not configured (missing bot token or chat id); skipping alert.")
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            message = None
            if telegraph.is_enabled():
                try:
                    page_url = await telegraph.create_page(client, paper)
                    message = format_teaser_message(paper, page_url)
                except Exception as e:
                    logger.warning(
                        f"Telegraph page failed for PMID {pmid} ({type(e).__name__}: {e}); "
                        "sending the full alert directly instead"
                    )
            if message is None:
                message = format_alert_message(paper)
            await _send_message(client, token, chat, message)
        return True
    except Exception as e:
        logger.warning(f"Telegram alert failed for PMID {pmid}: {type(e).__name__}: {e}")
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
