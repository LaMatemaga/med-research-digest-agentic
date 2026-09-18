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


def _truncate_plain_to_visible(plain: str, wrap, max_chars: int) -> str:
    """Return plain text such that visible_length(wrap(escaped plain)) <= max_chars.

    Cuts at a word boundary with an ellipsis when the full string does not fit. Escaping
    happens only after the plain-text cut, so HTML entities are never split.
    """
    if visible_length(wrap(_esc(plain))) <= max_chars:
        return plain
    lo, hi = 1, len(plain)
    best = "…"
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = truncate_at_word(plain, mid)
        if visible_length(wrap(_esc(candidate))) <= max_chars:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    if visible_length(wrap(_esc(best))) <= max_chars:
        return best
    return ""


def format_alert_message(paper: dict, max_chars: int = TELEGRAM_MAX_CHARS) -> str:
    """Full alert sent directly when Telegraph is unavailable. Stays within max_chars of
    visible text by dropping whole sentences; an oversized single sentence is hard-truncated
    at a word boundary with an ellipsis. Plain text is escaped only after the cut is chosen."""
    journal_line = " · ".join(p for p in (paper.get("journal", ""), paper.get("pub_date", "")) if p)
    head = _header_lines(paper)
    if journal_line:
        head.insert(1, f"<i>{_esc(journal_line)}</i>")

    pubmed: list[str] = []
    if paper.get("url"):
        pubmed = ["", f'<a href="{_href(paper["url"])}">Read the abstract on PubMed</a>']

    trials_tail: list[str] = []
    trials = [t for t in paper.get("matching_trials") or [] if t.get("nct_id")]
    if trials:
        trials_tail = ["", "<b>Matching trials</b>"]
        for t in trials:
            link = f'<a href="{_href(t["url"])}">{_esc(t["nct_id"])}</a>' if t.get("url") else _esc(t["nct_id"])
            title = f" — {_esc(truncate_at_word(t['title'], 120))}" if t.get("title") else ""
            trials_tail.append(f"• {link}{title}")

    sentences = _synthesis_sentences(paper)
    takeaway = sentences[-1] if sentences else None
    points = sentences[:-1] if sentences else []

    def build(takeaway_text: str | None, bullets: list[str], tail: list[str]) -> str:
        body: list[str] = []
        if takeaway_text is not None:
            body = ["", f"<b>Takeaway:</b> {_esc(takeaway_text)}", ""] + bullets
        return "\n".join(head + body + tail)

    def fits(takeaway_text: str | None, bullets: list[str], tail: list[str]) -> bool:
        return visible_length(build(takeaway_text, bullets, tail)) <= max_chars

    # Prefer keeping the PubMed link; include trials only when they still fit.
    candidate_tails = [trials_tail + pubmed, pubmed, []] if trials_tail else [pubmed, []]

    if takeaway is None:
        for tail in candidate_tails:
            if fits(None, [], tail):
                return build(None, [], tail)
        return build(None, [], [])

    fitted = takeaway
    chosen_tail = pubmed
    for tail in candidate_tails:
        if fits(takeaway, [], tail):
            fitted = takeaway
            chosen_tail = tail
            break
    else:
        # One sentence alone does not fit — hard-truncate at a word boundary.
        for tail in candidate_tails:
            fitted = _truncate_plain_to_visible(
                takeaway,
                lambda t, _tail=tail: build(t, [], _tail),
                max_chars,
            )
            if fitted and fits(fitted, [], tail):
                chosen_tail = tail
                break
        else:
            fitted = _truncate_plain_to_visible(takeaway, lambda t: build(t, [], []), max_chars)
            chosen_tail = []

    included: list[str] = []
    for i, sentence in enumerate(points):
        remaining = len(points) - i - 1
        note = [f"<i>(+{remaining} more in today's digest)</i>"] if remaining else []
        placed = False
        for tail in candidate_tails:
            if fits(fitted, included + [f"• {_esc(sentence)}"] + note, tail):
                included.append(f"• {_esc(sentence)}")
                chosen_tail = tail
                placed = True
                break
        if placed:
            continue
        omitted = len(points) - len(included)
        note_line = [f"<i>(+{omitted} more in today's digest)</i>"]
        for tail in candidate_tails:
            if fits(fitted, included + note_line, tail):
                included = included + note_line
                chosen_tail = tail
                break
        break

    for tail in candidate_tails:
        if fits(fitted, included, tail):
            chosen_tail = tail
            break

    return build(fitted, included, chosen_tail)


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
