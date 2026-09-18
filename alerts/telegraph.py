"""Full-text alert pages on Telegraph (telegra.ph), linked from a short Telegram teaser.

API facts used here, from https://telegra.ph/api:
- Every response is {"ok": true, "result": ...} or {"ok": false, "error": "..."}.
- createAccount(short_name 1-32 req, author_name 0-128) -> Account; `access_token` is only
  returned on creation, so it is cached (TELEGRAPH_ACCESS_TOKEN env, else app_state).
- createPage(access_token, title 1-256, author_name, content: Array of Node up to 64 KB,
  sent JSON-encoded like the docs' sample) -> Page with `url`.
- Node = str | {"tag", "attrs"?, "children"?}; tags limited to ALLOWED_TAGS (no h1/h2),
  attrs limited to href/src.
"""

import json
import logging
import os

import httpx

from alerts.text import split_sentences, strip_markdown, truncate_at_word
from storage import app_state

logger = logging.getLogger(__name__)

TELEGRAPH_API_BASE = "https://api.telegra.ph"
ACCOUNT_SHORT_NAME = "med-digest"
AUTHOR_NAME = "med-research-digest"
TOKEN_STATE_KEY = "telegraph_access_token"

ALLOWED_TAGS = frozenset(
    "a aside b blockquote br code em figcaption figure h3 h4 hr i iframe img li ol p pre s strong u ul video".split()
)
ALLOWED_ATTRS = frozenset({"href", "src"})
MAX_CONTENT_BYTES = 64 * 1024
MAX_TITLE_CHARS = 256


class TelegraphError(Exception):
    pass


def is_enabled() -> bool:
    return os.getenv("TELEGRAPH_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


# --- content ---------------------------------------------------------------------


def _el(tag: str, *children, href: str | None = None) -> dict:
    node: dict = {"tag": tag}
    if href:
        node["attrs"] = {"href": href}
    if children:
        node["children"] = [c for c in children if c not in (None, "")]
    return node


def build_page_content(paper: dict) -> list:
    """The full, untruncated alert as Telegraph Nodes."""
    journal = paper.get("journal", "")
    pub_date = paper.get("pub_date", "")
    authors = paper.get("authors") or []
    synthesis = strip_markdown(paper.get("synthesis") or paper.get("abstract") or "")
    url = paper.get("url", "")

    content: list = []
    byline = " · ".join(p for p in (journal, pub_date) if p)
    if byline:
        content.append(_el("p", _el("i", byline)))
    if authors:
        author_line = ", ".join(authors[:6]) + (" et al." if len(authors) > 6 else "")
        content.append(_el("p", author_line))

    grade = f"Level {paper.get('evidence_level', 'C')} ({paper.get('study_design', 'unknown')})"
    content.append(_el("p", _el("b", "Evidence: "), grade))
    content.append(
        _el(
            "p",
            _el("b", "Relevance: "),
            f"{paper.get('relevance_score', '?')}/10",
            f" · actionability {paper['clinical_actionability']}" if paper.get("clinical_actionability") else None,
        )
    )
    flags = paper.get("methodology_flags") or []
    if flags:
        content.append(_el("p", _el("b", "Methodology flags: "), ", ".join(flags)))

    sentences = split_sentences(synthesis)
    if sentences:
        content.append(_el("h3", "Key takeaway"))
        content.append(_el("blockquote", sentences[-1]))
        content.append(_el("h3", "Summary"))
        for paragraph in (p.strip() for p in synthesis.split("\n\n")):
            if paragraph:
                content.append(_el("p", " ".join(paragraph.split())))

    if paper.get("relevance_reasoning"):
        content.append(_el("h4", "Why this is relevant to you"))
        content.append(_el("p", paper["relevance_reasoning"]))

    trials = [t for t in paper.get("matching_trials") or [] if t.get("nct_id")]
    if trials:
        content.append(_el("h3", "Matching clinical trials"))
        items = []
        for t in trials:
            meta = ", ".join(x for x in (t.get("status"), t.get("phase")) if x)
            items.append(
                _el(
                    "li",
                    _el("a", t["nct_id"], href=t.get("url")),
                    f" — {t['title']}" if t.get("title") else None,
                    f" ({meta})" if meta else None,
                )
            )
        content.append(_el("ul", *items))

    if url:
        content.append(_el("h3", "Source"))
        content.append(_el("p", _el("a", "Read the abstract on PubMed", href=url)))
    return content


def validate_content(content: list) -> None:
    """Raises TelegraphError if content would violate the documented Node schema or size."""

    def walk(node) -> None:
        if isinstance(node, str):
            return
        if not isinstance(node, dict) or node.get("tag") not in ALLOWED_TAGS:
            raise TelegraphError(f"invalid node {node!r}")
        if set(node) - {"tag", "attrs", "children"}:
            raise TelegraphError(f"unexpected keys in node {node!r}")
        if set(node.get("attrs", {})) - ALLOWED_ATTRS:
            raise TelegraphError(f"unsupported attrs in node {node!r}")
        for child in node.get("children", []):
            walk(child)

    if not content:
        raise TelegraphError("empty content")
    for node in content:
        walk(node)
    size = len(json.dumps(content, ensure_ascii=False).encode("utf-8"))
    if size > MAX_CONTENT_BYTES:
        raise TelegraphError(f"content is {size} bytes, over the 64 KB limit")


# --- API -----------------------------------------------------------------------------


async def _call(client: httpx.AsyncClient, method: str, data: dict) -> dict:
    resp = await client.post(f"{TELEGRAPH_API_BASE}/{method}", data=data)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise TelegraphError(f"{method} failed: {body.get('error', 'unknown error')}")
    return body["result"]


async def get_access_token(client: httpx.AsyncClient) -> str:
    """TELEGRAPH_ACCESS_TOKEN env, else the cached token, else a new account (cached)."""
    token = os.getenv("TELEGRAPH_ACCESS_TOKEN") or app_state.get_value(TOKEN_STATE_KEY)
    if token:
        return token
    account = await _call(client, "createAccount", {"short_name": ACCOUNT_SHORT_NAME, "author_name": AUTHOR_NAME})
    token = account.get("access_token")
    if not token:
        raise TelegraphError("createAccount returned no access_token")
    app_state.set_value(TOKEN_STATE_KEY, token)
    logger.info("Created a Telegraph account and cached its access token in data/seen_items.db")
    return token


async def create_page(client: httpx.AsyncClient, paper: dict) -> str:
    """Publishes the full alert page and returns its URL. Raises on any failure."""
    content = build_page_content(paper)
    validate_content(content)
    token = await get_access_token(client)
    page = await _call(
        client,
        "createPage",
        {
            "access_token": token,
            "title": truncate_at_word(paper.get("title") or "Untitled", MAX_TITLE_CHARS),
            "author_name": AUTHOR_NAME,
            "content": json.dumps(content, ensure_ascii=False),
        },
    )
    if not page.get("url"):
        raise TelegraphError("createPage returned no url")
    return page["url"]
