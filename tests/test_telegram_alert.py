import json
import re

import httpx
import pytest

from alerts import telegram, telegraph
from alerts.text import split_sentences
from storage import app_state, seen_store
from tests.fakes import FakeHttp, FakeResponse

SYNTHESIS = (
    "**Drug X** reduced all-cause mortality by 18% (HR 0.82, 95% CI 0.71-0.94) vs. placebo in 6,263 patients. "
    "The trial was large, multi-center and pre-registered, although funded by the manufacturer. "
    "Benefits were consistent across subgroups, per Smith et al. in the supplement. "
    "Clinicians should consider Drug X for eligible patients with HFmrEF."
)


def _paper(**overrides) -> dict:
    base = {
        "pmid": "111",
        "title": "A Trial of Drug X in Heart Failure <HFmrEF> & Beyond",
        "journal": "NEJM",
        "pub_date": "2026-03",
        "authors": ["Smith J", "Doe A"],
        "evidence_level": "A",
        "study_design": "RCT",
        "relevance_score": 9,
        "clinical_actionability": "high",
        "relevance_reasoning": "Core subspecialty.",
        "synthesis": SYNTHESIS,
        "url": "https://pubmed.ncbi.nlm.nih.gov/111/",
        "matching_trials": [
            {"nct_id": "NCT01234567", "title": "Drug X Outcomes", "status": "COMPLETED", "phase": "PHASE3", "url": "https://clinicaltrials.gov/study/NCT01234567"}
        ],
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(seen_store, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.delenv("TELEGRAPH_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAPH_ENABLED", raising=False)


@pytest.fixture
def http(monkeypatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(httpx, "AsyncClient", fake.client_class())
    return fake


# --- sentence splitting ---------------------------------------------------------------


def test_split_sentences_keeps_decimals_and_abbreviations_intact():
    sentences = split_sentences("HR 0.82 vs. placebo was seen. Smith et al. agreed, e.g. in HFpEF. HFpEF differs.")
    assert sentences == ["HR 0.82 vs. placebo was seen.", "Smith et al. agreed, e.g. in HFpEF.", "HFpEF differs."]


# --- happy path: Telegraph page + short teaser ------------------------------------------


async def test_send_alert_creates_telegraph_page_and_sends_html_teaser(http):
    ok = await telegram.send_alert(_paper(), bot_token="bot-token", chat_id="12345")

    assert ok is True
    [page_call] = http.calls_to("/createPage")
    assert page_call["data"]["access_token"] == "tg-token-1"
    [send] = http.calls_to("/sendMessage")
    assert send["url"] == "https://api.telegram.org/botbot-token/sendMessage"
    payload = send["json"]
    assert payload["chat_id"] == "12345"
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": True}
    text = payload["text"]
    assert "<b>A Trial of Drug X in Heart Failure &lt;HFmrEF&gt; &amp; Beyond</b>" in text
    assert '<a href="https://telegra.ph/Test-Page-09-17">📄 Leer análisis completo</a>' in text
    assert "Drug X reduced all-cause mortality" in text  # one-sentence hook
    assert "Clinicians should consider" not in text  # the rest lives on the page
    assert "**" not in text
    assert telegram.visible_length(text) < 1000


async def test_telegraph_disabled_by_env_sends_full_message(http, monkeypatch):
    monkeypatch.setenv("TELEGRAPH_ENABLED", "false")

    assert await telegram.send_alert(_paper(), bot_token="t", chat_id="c") is True
    assert http.calls_to("/createPage") == []
    assert "Clinicians should consider Drug X" in http.calls_to("/sendMessage")[0]["json"]["text"]


# --- fallback: Telegraph failure must still deliver the alert ---------------------------


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout("telegraph down"),
        FakeResponse(200, {"ok": False, "error": "CONTENT_TOO_BIG"}),
        FakeResponse(502, {"ok": False, "error": "bad gateway"}),
    ],
    ids=["network", "api-error", "http-502"],
)
async def test_telegraph_failure_falls_back_to_full_message(http, failure, caplog):
    http.telegraph_fails = failure

    ok = await telegram.send_alert(_paper(), bot_token="t", chat_id="c")

    assert ok is True
    [send] = http.calls_to("/sendMessage")
    text = send["json"]["text"]
    assert send["json"]["parse_mode"] == "HTML"
    assert "telegra.ph" not in text
    assert "<b>Takeaway:</b> Clinicians should consider Drug X for eligible patients with HFmrEF." in text
    assert "NCT01234567" in text and "https://pubmed.ncbi.nlm.nih.gov/111/" in text
    assert any("sending the full alert directly instead" in r.getMessage() for r in caplog.records)


async def test_html_parse_rejection_is_resent_as_plain_text(http):
    http.telegraph_fails = httpx.ConnectTimeout("down")
    http.telegram_responses = [
        FakeResponse(400, {"ok": False, "error_code": 400, "description": "Bad Request: can't parse entities: x"}),
        FakeResponse(200, {"ok": True, "result": {}}),
    ]

    assert await telegram.send_alert(_paper(), bot_token="t", chat_id="c") is True

    first, second = http.calls_to("/sendMessage")
    assert first["json"]["parse_mode"] == "HTML"
    assert "parse_mode" not in second["json"]
    assert "<b>" not in second["json"]["text"] and "<HFmrEF> & Beyond" in second["json"]["text"]


async def test_telegram_failure_returns_false(http):
    http.telegram_responses = [FakeResponse(500, {"ok": False})]
    assert await telegram.send_alert(_paper(), bot_token="t", chat_id="c") is False


async def test_send_alert_returns_false_when_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert await telegram.send_alert(_paper()) is False


# --- fallback message never cuts a sentence and respects the real limit -----------------


def test_long_fallback_message_fits_4096_and_ends_on_whole_sentences():
    sentences = [f"Finding number {i} shows a consistent effect across 12.5% of the cohort in this analysis." for i in range(80)]
    paper = _paper(synthesis=" ".join(sentences))

    text = telegram.format_alert_message(paper)

    assert telegram.visible_length(text) <= telegram.TELEGRAM_MAX_CHARS
    bullets = [line[2:] for line in text.splitlines() if line.startswith("• ") and "NCT" not in line]
    assert bullets and all(b in sentences for b in bullets)  # every bullet is a whole sentence
    assert "more in today's digest)" in text
    assert f"<b>Takeaway:</b> {sentences[-1]}" in text
    assert "https://pubmed.ncbi.nlm.nih.gov/111/" in text


def test_single_oversized_sentence_takeaway_fits_4096():
    """A 5000-char single-sentence synthesis must be hard-truncated, not pasted whole."""
    paper = _paper(synthesis="Word " * 1000, matching_trials=[])  # ~5000 chars, one sentence

    text = telegram.format_alert_message(paper)

    assert telegram.visible_length(text) <= telegram.TELEGRAM_MAX_CHARS
    assert "<b>Takeaway:</b>" in text
    assert "…" in text
    assert "https://pubmed.ncbi.nlm.nih.gov/111/" in text
    assert "Word " * 1000 not in text


def test_very_long_title_fallback_fits_4096():
    paper = _paper(title=("Heart failure outcomes " * 200).strip(), matching_trials=[])

    text = telegram.format_alert_message(paper)

    assert telegram.visible_length(text) <= telegram.TELEGRAM_MAX_CHARS
    assert "https://pubmed.ncbi.nlm.nih.gov/111/" in text


def test_special_chars_near_truncation_are_not_split_inside_entities():
    # Dense & / < near the cut so a naive mid-entity slice would yield broken &amp; / &lt;.
    chunk = "alpha & beta < gamma > delta "
    paper = _paper(synthesis=chunk * 200, matching_trials=[])

    text = telegram.format_alert_message(paper)

    assert telegram.visible_length(text) <= telegram.TELEGRAM_MAX_CHARS
    # Every & in the HTML is a complete entity (never a sliced &amp; / &lt;).
    assert not re.search(r"&(?!(?:amp|lt|gt|quot);)", text)
    assert "https://pubmed.ncbi.nlm.nih.gov/111/" in text


def test_visible_length_counts_after_entity_parsing_in_utf16():
    assert telegram.visible_length("<b>a&amp;b</b>") == 3
    assert telegram.visible_length("📄") == 2


# --- Telegraph content nodes -------------------------------------------------------------


def _walk(nodes):
    for n in nodes:
        yield n
        if isinstance(n, dict):
            yield from _walk(n.get("children", []))


def test_page_content_uses_only_documented_nodes_and_carries_full_text():
    content = telegraph.build_page_content(_paper())

    telegraph.validate_content(content)
    elements = [n for n in _walk(content) if isinstance(n, dict)]
    assert {e["tag"] for e in elements} <= telegraph.ALLOWED_TAGS
    assert all(set(e.get("attrs", {})) <= {"href", "src"} for e in elements)
    text = json.dumps(content, ensure_ascii=False)
    assert "Clinicians should consider Drug X for eligible patients with HFmrEF." in text  # untruncated
    assert "Level A (RCT)" in text and "9/10" in text and "NEJM · 2026-03" in text
    assert {"tag": "a", "attrs": {"href": "https://clinicaltrials.gov/study/NCT01234567"}, "children": ["NCT01234567"]} in elements
    assert {"tag": "a", "attrs": {"href": "https://pubmed.ncbi.nlm.nih.gov/111/"}, "children": ["Read the abstract on PubMed"]} in elements
    assert "**" not in text


def test_page_content_omits_trials_section_when_none():
    content = telegraph.build_page_content(_paper(matching_trials=[]))
    assert "Matching clinical trials" not in json.dumps(content)


def test_validate_content_rejects_undocumented_tags_attrs_and_oversize():
    for bad in ([{"tag": "h1", "children": ["x"]}], [{"tag": "a", "attrs": {"class": "x"}}], [{"tag": "p", "style": "x"}]):
        with pytest.raises(telegraph.TelegraphError):
            telegraph.validate_content(bad)
    with pytest.raises(telegraph.TelegraphError):
        telegraph.validate_content([{"tag": "p", "children": ["x" * (65 * 1024)]}])


async def test_create_page_sends_documented_params(http):
    url = await telegraph.create_page(httpx.AsyncClient(), _paper())

    assert url == "https://telegra.ph/Test-Page-09-17"
    [call] = http.calls_to("/createPage")
    data = call["data"]
    assert set(data) == {"access_token", "title", "author_name", "content"}
    assert data["title"] == _paper()["title"] and data["author_name"] == "med-research-digest"
    assert json.loads(data["content"]) == telegraph.build_page_content(_paper())


# --- access token caching ----------------------------------------------------------------


async def test_account_created_once_and_token_reused_across_alerts(http):
    for _ in range(3):
        assert await telegram.send_alert(_paper(), bot_token="t", chat_id="c") is True

    [account_call] = http.calls_to("/createAccount")
    assert account_call["data"] == {"short_name": "med-digest", "author_name": "med-research-digest"}
    assert len(http.calls_to("/createPage")) == 3
    assert all(c["data"]["access_token"] == "tg-token-1" for c in http.calls_to("/createPage"))
    assert app_state.get_value(telegraph.TOKEN_STATE_KEY) == "tg-token-1"


async def test_cached_token_survives_new_process_state(http):
    app_state.set_value(telegraph.TOKEN_STATE_KEY, "persisted-token")

    await telegram.send_alert(_paper(), bot_token="t", chat_id="c")

    assert http.calls_to("/createAccount") == []
    assert http.calls_to("/createPage")[0]["data"]["access_token"] == "persisted-token"


async def test_env_token_overrides_cache(http, monkeypatch):
    app_state.set_value(telegraph.TOKEN_STATE_KEY, "cached")
    monkeypatch.setenv("TELEGRAPH_ACCESS_TOKEN", "from-env")

    await telegram.send_alert(_paper(), bot_token="t", chat_id="c")

    assert http.calls_to("/createPage")[0]["data"]["access_token"] == "from-env"


async def test_send_alerts_for_tier_only_sends_top_tier(monkeypatch):
    sent = []

    async def _fake_send_alert(paper, bot_token=None, chat_id=None):
        sent.append(paper["pmid"])
        return True

    monkeypatch.setattr(telegram, "send_alert", _fake_send_alert)
    papers = [{"pmid": "1", "tier": "🔴"}, {"pmid": "2", "tier": "🟡"}, {"pmid": "3", "tier": "🔴"}]

    assert await telegram.send_alerts_for_tier(papers, tier_fn=lambda p: p["tier"]) == ["1", "3"]
    assert sent == ["1", "3"]
