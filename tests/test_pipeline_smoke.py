"""End-to-end smoke test: the full 7-stage pipeline (discovery -> grading ->
relevance filter -> trials cross-check -> voices -> synthesis -> format/alert)
must run without crashing, using only mocks — no network, no real API keys,
no npx/MCP subprocess, no `claude` CLI."""

import json

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

import main
import pipeline.stage2_evidence_grader as stage2
import pipeline.stage3_relevance_filter as stage3
import pipeline.stage3b_trials_crosscheck as stage3b
import pipeline.stage1_discovery as stage1
from alerts import telegram
from storage import seen_store
from voices import clinician, gremial, methodologist, specialist


def _text_query(payload: dict):
    async def _fake_query(*, prompt, options):
        yield AssistantMessage(
            content=[TextBlock(text=json.dumps(payload))],
            model="claude-haiku-4-5-20251001",
        )

    return _fake_query


def _prose_query(text: str):
    async def _fake_query(*, prompt, options):
        yield AssistantMessage(content=[TextBlock(text=text)], model="claude-haiku-4-5-20251001")

    return _fake_query


class _FakePubMedMCPClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def search_and_fetch(self, query_spec):
        return [
            {
                "pmid": "999",
                "title": "A Landmark Trial in Heart Failure",
                "authors": ["Smith J"],
                "journal": "NEJM",
                "pub_date": "2026-03",
                "abstract": "A large RCT of drug Y in heart failure showed a mortality benefit.",
                "publication_types": ["Randomized Controlled Trial"],
                "mesh_terms": ["Heart Failure"],
                "source_specialty": query_spec["specialty"],
                "url": "https://pubmed.ncbi.nlm.nih.gov/999/",
            }
        ]


@pytest.fixture
def isolated_seen_db(tmp_path, monkeypatch):
    db_path = tmp_path / "seen_items.db"
    monkeypatch.setattr(seen_store, "DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture(autouse=True)
def _mock_all_external_calls(monkeypatch, isolated_seen_db, tmp_path):
    # Stage 1: PubMed MCP discovery -> canned papers, no npx/subprocess involved.
    monkeypatch.setattr(stage1, "PubMedMCPClient", _FakePubMedMCPClient)

    # Stage 2: evidence grader -> deterministic grade.
    monkeypatch.setattr(
        stage2,
        "query",
        _text_query(
            {
                "study_design": "RCT",
                "evidence_level": "A",
                "methodology_flags": [],
                "grader_reasoning": "Large multi-center RCT.",
            }
        ),
    )

    # Stage 3: relevance filter -> high score so the paper survives and hits tier 🔴.
    monkeypatch.setattr(
        stage3,
        "query",
        _text_query(
            {
                "relevance_score": 9,
                "relevance_reasoning": "Core subspecialty, practice-changing.",
                "clinical_actionability": "high",
            }
        ),
    )

    # Stage 3b: clinical trials cross-check -> no real MCP call.
    async def _fake_find_matching_trials(paper, max_results=3):
        return [{"nct_id": "NCT00000001", "title": "Companion trial", "status": "Recruiting", "phase": "Phase 3", "url": "https://clinicaltrials.gov/study/NCT00000001"}]

    monkeypatch.setattr(stage3b, "find_matching_trials", _fake_find_matching_trials)

    # Stage 4: voices active for role=clinician + specialty=cardiology.
    monkeypatch.setattr(clinician, "query", _prose_query("Plain-language clinical takeaway."))
    monkeypatch.setattr(methodologist, "query", _prose_query("Methodology looks sound."))
    monkeypatch.setattr(gremial, "query", _prose_query("Relevant society guidance is pending."))
    monkeypatch.setattr(specialist, "query", _prose_query("Cardiology-specific nuance."))

    # Stage 5: synthesizer.
    import pipeline.stage5_synthesizer as stage5

    monkeypatch.setattr(stage5, "query", _prose_query("This RCT shows a mortality benefit for drug Y."))

    # Stage 6 + alerting: no real Telegram network call.
    class _FakeAsyncClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, json=None):
            class _Resp:
                def raise_for_status(self):
                    return None

            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    # Redirect saved output to a temp dir instead of the real outputs/ folder.
    def _fake_save_newsletter(content, output_dir="outputs"):
        out = tmp_path / "outputs" / "digest.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")
        return str(out)

    monkeypatch.setattr(main, "save_newsletter", _fake_save_newsletter)


async def test_full_pipeline_runs_end_to_end_without_network_or_real_keys(monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "PROFILE",
        {
            "name": "Dr. Test",
            "specialties": ["cardiology"],
            "subspecialties": [],
            "role": ["clinician"],
            "practice_setting": "academic",
            "country": "MX",
            "research_interests": [],
            "credentials": ["MD"],
            "patient_population": "adult",
            "newsletter_preferences": {
                "max_papers": 5,
                "min_evidence_level": "pilot",
                "include_preprints": False,
                "language": "english",
            },
        },
    )

    await main.run_pipeline(days=7, specialty_override=None, dry_run=False, reset_seen=True)

    out = capsys.readouterr().out
    assert "med-research-digest complete" in out
    assert "Telegram alerts:" in out


async def test_second_run_finds_nothing_new(monkeypatch, capsys):
    profile = {
        "name": "Dr. Test",
        "specialties": ["cardiology"],
        "subspecialties": [],
        "role": ["clinician"],
        "practice_setting": "academic",
        "country": "MX",
        "research_interests": [],
        "credentials": ["MD"],
        "patient_population": "adult",
        "newsletter_preferences": {
            "max_papers": 5,
            "min_evidence_level": "pilot",
            "include_preprints": False,
            "language": "english",
        },
    }
    monkeypatch.setattr(main, "PROFILE", profile)

    await main.run_pipeline(days=7, specialty_override=None, dry_run=False, reset_seen=True)
    await main.run_pipeline(days=7, specialty_override=None, dry_run=False, reset_seen=False)

    out = capsys.readouterr().out
    assert "No new papers since the last run" in out
