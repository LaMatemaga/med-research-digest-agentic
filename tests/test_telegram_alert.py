import httpx
import pytest

from alerts import telegram


def _paper(**overrides) -> dict:
    base = {
        "pmid": "111",
        "title": "A Trial of Drug X in Heart Failure",
        "journal": "NEJM",
        "pub_date": "2026-03",
        "evidence_level": "A",
        "study_design": "RCT",
        "relevance_score": 9,
        "synthesis": "**Drug X** reduces mortality. See [details](https://example.com).",
        "url": "https://pubmed.ncbi.nlm.nih.gov/111/",
        "matching_trials": [{"nct_id": "NCT01234567"}],
    }
    base.update(overrides)
    return base


def test_format_alert_message_strips_markdown_and_includes_key_fields():
    message = telegram.format_alert_message(_paper())

    assert "**" not in message
    assert "[details]" not in message
    assert "Drug X" in message
    assert "NCT01234567" in message
    assert "https://pubmed.ncbi.nlm.nih.gov/111/" in message
    assert "9/10" in message


def test_format_alert_message_truncates_long_synthesis():
    long_synthesis = "x" * 900
    message = telegram.format_alert_message(_paper(synthesis=long_synthesis))

    assert "x" * 900 not in message
    assert "..." in message


async def test_send_alert_returns_false_when_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    ok = await telegram.send_alert(_paper())

    assert ok is False


async def test_send_alert_posts_expected_payload_to_bot_api(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    ok = await telegram.send_alert(_paper(), bot_token="test-token", chat_id="12345")

    assert ok is True
    assert captured["url"] == "https://api.telegram.org/bottest-token/sendMessage"
    assert captured["json"]["chat_id"] == "12345"
    assert "Drug X" in captured["json"]["text"]
    assert "**" not in captured["json"]["text"]


async def test_send_alert_returns_false_on_http_error(monkeypatch):
    class _FailingAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, json=None):
            raise httpx.ConnectTimeout("boom")

    monkeypatch.setattr(httpx, "AsyncClient", _FailingAsyncClient)

    ok = await telegram.send_alert(_paper(), bot_token="test-token", chat_id="12345")

    assert ok is False


async def test_send_alerts_for_tier_only_sends_top_tier(monkeypatch):
    sent = []

    async def _fake_send_alert(paper, bot_token=None, chat_id=None):
        sent.append(paper["pmid"])
        return True

    monkeypatch.setattr(telegram, "send_alert", _fake_send_alert)

    papers = [
        {"pmid": "1", "tier": "🔴"},
        {"pmid": "2", "tier": "🟡"},
        {"pmid": "3", "tier": "🔴"},
    ]

    alerted = await telegram.send_alerts_for_tier(papers, tier_fn=lambda p: p["tier"])

    assert alerted == ["1", "3"]
    assert sent == ["1", "3"]
