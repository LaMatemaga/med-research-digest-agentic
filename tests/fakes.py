"""A fake httpx.AsyncClient that routes Telegraph and Telegram Bot API calls by URL,
records every request, and never touches the network."""

import json as jsonlib


class FakeResponse:
    def __init__(self, status_code: int = 200, body: dict | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": {}}
        self.text = jsonlib.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=None)


class FakeHttp:
    """Configure behavior, then `monkeypatch.setattr(httpx, "AsyncClient", fake.client_class())`."""

    def __init__(self):
        self.calls: list[dict] = []
        self.telegraph_token = "tg-token-1"
        self.telegraph_page_url = "https://telegra.ph/Test-Page-09-17"
        self.telegraph_fails: Exception | FakeResponse | None = None
        self.telegram_responses: list[FakeResponse] = []

    def client_class(self):
        fake = self

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def post(self, url, json=None, data=None):
                return fake._handle(url, json=json, data=data)

        return _Client

    def _handle(self, url, json=None, data=None) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "data": data})
        if url.startswith("https://api.telegra.ph/"):
            if isinstance(self.telegraph_fails, Exception):
                raise self.telegraph_fails
            if isinstance(self.telegraph_fails, FakeResponse):
                return self.telegraph_fails
            if url.endswith("/createAccount"):
                return FakeResponse(body={"ok": True, "result": {"short_name": "med-digest", "access_token": self.telegraph_token}})
            if url.endswith("/createPage"):
                return FakeResponse(body={"ok": True, "result": {"path": "Test-Page-09-17", "url": self.telegraph_page_url}})
        if "/sendMessage" in url:
            return self.telegram_responses.pop(0) if self.telegram_responses else FakeResponse(body={"ok": True, "result": {}})
        raise AssertionError(f"unexpected URL {url}")

    def calls_to(self, suffix: str) -> list[dict]:
        return [c for c in self.calls if c["url"].endswith(suffix)]
