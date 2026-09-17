"""Every per-paper fan-out must respect its concurrency cap. Each Claude Agent SDK call
spawns a `claude` CLI (plus npx MCP servers on MCP-enabled stages); unbounded fan-out
over ~133 papers exhausted memory on Windows (WinError 1455)."""

import asyncio
import json

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

import pipeline.stage1_discovery as stage1
import pipeline.stage2_evidence_grader as stage2
import pipeline.stage3_relevance_filter as stage3
import pipeline.stage3b_trials_crosscheck as stage3b
import pipeline.stage4_voices as stage4
import pipeline.stage5_synthesizer as stage5
from utils.concurrency import gather_bounded

LIMIT = 3
N_PAPERS = 40


class InFlightTracker:
    def __init__(self):
        self.current = 0
        self.peak = 0
        self.calls = 0

    async def hold(self):
        self.current += 1
        self.calls += 1
        self.peak = max(self.peak, self.current)
        try:
            await asyncio.sleep(0.002)
        finally:
            self.current -= 1

    def fake_query(self, payload: dict | str):
        text = payload if isinstance(payload, str) else json.dumps(payload)

        async def _query(*, prompt, options):
            await self.hold()
            yield AssistantMessage(content=[TextBlock(text=text)], model="m")

        return _query


def _papers(n: int = N_PAPERS) -> list[dict]:
    return [{"pmid": str(i), "title": f"Paper {i}", "abstract": "x", "mesh_terms": ["Heart Failure"]} for i in range(n)]


def _assert_capped(tracker: InFlightTracker, expected_calls: int):
    assert tracker.calls == expected_calls
    assert tracker.peak == LIMIT, f"peak in-flight {tracker.peak}, limit {LIMIT}"


async def test_gather_bounded_preserves_order_and_exceptions_release_slots():
    tracker = InFlightTracker()

    async def work(i: int) -> int:
        await tracker.hold()
        if i % 5 == 0:
            raise ValueError(i)
        return i * 2

    results = await gather_bounded(work, range(20), LIMIT)

    assert tracker.peak == LIMIT
    for i, r in enumerate(results):
        if i % 5 == 0:
            assert isinstance(r, ValueError)
        else:
            assert r == i * 2


async def test_stage1_discovery_is_capped(monkeypatch):
    tracker = InFlightTracker()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def search_and_fetch(self, spec):
            await tracker.hold()
            return []

    specialties = ["cardiology", "oncology", "neurology", "psychiatry", "pediatrics", "surgery", "nephrology", "hematology"]
    monkeypatch.setattr(stage1, "PubMedMCPClient", _Client)
    monkeypatch.setattr(stage1, "MAX_CONCURRENT_DISCOVERY_CALLS", LIMIT)

    await stage1.run_discovery({"specialties": specialties, "role": []}, days=7)

    _assert_capped(tracker, len(specialties))


async def test_stage2_grading_is_capped(monkeypatch):
    tracker = InFlightTracker()
    monkeypatch.setattr(stage2, "query", tracker.fake_query({"study_design": "RCT", "evidence_level": "A"}))
    monkeypatch.setattr(stage2, "MAX_CONCURRENT_GRADING_CALLS", LIMIT)

    graded = await stage2.grade_all(_papers())

    _assert_capped(tracker, N_PAPERS)
    assert [p["pmid"] for p in graded] == [str(i) for i in range(N_PAPERS)]
    assert all(p["evidence_level"] == "A" for p in graded)


async def test_stage3_relevance_is_capped(monkeypatch):
    tracker = InFlightTracker()
    monkeypatch.setattr(stage3, "query", tracker.fake_query({"relevance_score": 7}))
    monkeypatch.setattr(stage3, "MAX_CONCURRENT_RELEVANCE_CALLS", LIMIT)

    kept, all_scored = await stage3.filter_papers(_papers(), {"newsletter_preferences": {"max_papers": 10}})

    _assert_capped(tracker, N_PAPERS)
    assert len(all_scored) == N_PAPERS and len(kept) == 10


async def test_stage3b_trials_is_capped(monkeypatch):
    tracker = InFlightTracker()

    async def _find(paper, max_results=3):
        await tracker.hold()
        return []

    monkeypatch.setattr(stage3b, "find_matching_trials", _find)
    monkeypatch.setattr(stage3b, "MAX_CONCURRENT_TRIALS_CALLS", LIMIT)

    await stage3b.crosscheck_trials(_papers())

    _assert_capped(tracker, N_PAPERS)


async def test_stage4_voices_per_paper_is_capped(monkeypatch):
    tracker = InFlightTracker()

    async def _voice(paper):
        await tracker.hold()
        return "ok"

    voices = {f"voice_{i}": _voice for i in range(10)}
    monkeypatch.setattr(stage4, "select_voices", lambda profile: voices)
    monkeypatch.setattr(stage4, "MAX_CONCURRENT_VOICE_CALLS", LIMIT)

    await stage4.run_all_voices(_papers(2), {})

    _assert_capped(tracker, 20)


async def test_stage5_synthesis_is_capped(monkeypatch):
    tracker = InFlightTracker()
    monkeypatch.setattr(stage5, "query", tracker.fake_query("A synthesized paragraph."))
    monkeypatch.setattr(stage5, "MAX_CONCURRENT_SYNTHESIS_CALLS", LIMIT)

    await stage5.synthesize_all(_papers())

    _assert_capped(tracker, N_PAPERS)


def test_default_cap_is_conservative_and_env_overridable(monkeypatch):
    import importlib

    import config

    monkeypatch.delenv("MAX_CONCURRENT_CLAUDE_CALLS", raising=False)
    assert importlib.reload(config).MAX_CONCURRENT_GRADING_CALLS == 4

    monkeypatch.setenv("MAX_CONCURRENT_CLAUDE_CALLS", "2")
    reloaded = importlib.reload(config)
    assert reloaded.MAX_CONCURRENT_GRADING_CALLS == 2
    assert reloaded.MAX_CONCURRENT_RELEVANCE_CALLS == 2

    monkeypatch.setenv("MAX_CONCURRENT_CLAUDE_CALLS", "0")
    assert importlib.reload(config).MAX_CONCURRENT_CLAUDE_CALLS == 1

    monkeypatch.delenv("MAX_CONCURRENT_CLAUDE_CALLS")
    importlib.reload(config)


@pytest.mark.parametrize(
    "module",
    ["voices.clinician", "voices.specialist", "voices.methodologist", "voices.gremial", "voices.researcher", "voices.educator", "pipeline.stage5_synthesizer"],
)
def test_non_tool_stages_do_not_attach_mcp_servers(module):
    """Every attached MCP server is another npx subprocess per call; stages that never
    call tools must not carry one."""
    import importlib
    import inspect

    source = inspect.getsource(importlib.import_module(module))
    assert "mcp_servers" not in source
