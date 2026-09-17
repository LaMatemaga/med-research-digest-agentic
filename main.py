import asyncio
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from context.physician import PROFILE
from pipeline.stage1_discovery import run_discovery
from pipeline.stage2_evidence_grader import grade_all
from pipeline.stage3_relevance_filter import filter_papers
from pipeline.stage3b_trials_crosscheck import crosscheck_trials
from pipeline.stage4_voices import run_all_voices
from pipeline.stage5_synthesizer import synthesize_all
from pipeline.stage6_formatter import assign_tier, format_newsletter, save_newsletter
from storage import seen_store
from alerts.telegram import send_alerts_for_tier


async def run_pipeline(
    days: int,
    specialty_override: str | None,
    dry_run: bool,
    reset_seen: bool = False,
) -> None:
    today = date.today()
    start = today - timedelta(days=days)
    date_range = f"{start.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}"

    print(f"\nmed-research-digest | {today} | last {days} days")
    if specialty_override:
        print(f"  Specialty override: {specialty_override}")
    print()

    seen_store.init_db()
    if reset_seen:
        print("  --reset-seen: clearing seen-items memory\n")
        seen_store.reset()

    # Stage 1: Discovery
    print("Stage 1: Discovery (PubMed via MCP)...")
    papers = await run_discovery(PROFILE, days, specialty_override, dry_run)
    if dry_run:
        return

    n_found = len(papers)
    if not papers:
        print("\nNo papers found. Try --days 30 or add specialties to context/physician.py.")
        return

    papers = seen_store.filter_new(papers)
    print(f"  Memory: {n_found} discovered -> {len(papers)} new since last run")
    if not papers:
        print("\nNo new papers since the last run. Use --reset-seen to see everything again.")
        return

    # Stage 2: Evidence grading
    print(f"\nStage 2: Grading evidence levels for {n_found} papers...")
    papers = await grade_all(papers)
    print(f"  Graded: {len(papers)} papers")

    # Stage 3: Relevance filter
    print(f"\nStage 3: Relevance filtering...")
    papers, all_scored = await filter_papers(papers, PROFILE)

    # Every paper that was actually graded and scored this run is now "processed" —
    # remember it whether or not it made the digest, so a paper discarded for low
    # relevance doesn't get rediscovered and rescored on every future run.
    seen_store.record_seen(all_scored)

    if not papers:
        print(
            "\nAll papers filtered out. Try --days 30, --specialty <name>, or "
            "lower min_evidence_level in context/physician.py."
        )
        return

    # Stage 3b: Clinical trials cross-check
    print(f"\nStage 3b: Cross-checking clinical trials for {len(papers)} papers...")
    papers = await crosscheck_trials(papers)

    # Stage 4: Voices
    print(f"\nStage 4: Running voices ({len(papers)} papers, voices run in parallel per paper)...")
    papers = await run_all_voices(papers, PROFILE)
    print(f"  Voices complete for {len(papers)} papers")

    # Stage 5: Synthesis
    print(f"\nStage 5: Synthesizing narratives...")
    papers = await synthesize_all(papers)
    print(f"  Synthesized: {len(papers)} papers")

    # Stage 6: Format and save
    print("\nStage 6: Formatting newsletter...")
    content = format_newsletter(papers, PROFILE, date_range)
    output_dir = str(Path(__file__).parent / "outputs")
    output_path = save_newsletter(content, output_dir)

    # Severity-gated Telegram alerting for the top tier only. "Seen" was already
    # recorded right after stage 3 — alerted_at is a distinct signal set ONLY on a
    # confirmed successful send, so a failed send never gets conflated with "seen"
    # and stays visible (via the warning send_alert logs, and the count below).
    alerted_pmids = await send_alerts_for_tier(papers, assign_tier, top_tier="🔴")
    for pmid in alerted_pmids:
        seen_store.mark_alerted(pmid)

    # Console summary
    tiers = [assign_tier(p) for p in papers]
    n_red = tiers.count("🔴")
    n_yellow = tiers.count("🟡")
    n_blue = tiers.count("🔵")

    print(f"\n{'=' * 50}")
    print(f"med-research-digest complete")
    print(f"  Papers found:    {n_found}")
    print(f"  Papers included: {len(papers)}")
    print(f"  🔴 High:         {n_red}")
    print(f"  🟡 Moderate:     {n_yellow}")
    print(f"  🔵 Watching:     {n_blue}")
    n_alert_failed = n_red - len(alerted_pmids)
    alert_summary = f"{len(alerted_pmids)}/{n_red} sent" if n_red else "0/0"
    if n_alert_failed > 0:
        alert_summary += f" ({n_alert_failed} FAILED — see warnings above, will not auto-retry)"
    print(f"  Telegram alerts: {alert_summary}")
    print(f"  Saved to:        {output_path}")
    print(f"{'=' * 50}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Medical research newsletter generator powered by PubMed + Claude",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                          # last 7 days, all profile specialties
  python main.py --days 30               # last 30 days
  python main.py --specialty cardiology  # override to cardiology only
  python main.py --dry-run               # show PubMed queries, don't fetch
  python main.py --reset-seen            # clear seen-items memory, show everything again
        """,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window in days (default: 7)",
    )
    parser.add_argument(
        "--specialty",
        type=str,
        default=None,
        metavar="SPECIALTY",
        help="Override specialty filter (e.g. cardiology, oncology)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show PubMed queries without fetching or calling Claude",
    )
    parser.add_argument(
        "--reset-seen",
        action="store_true",
        help="Clear the seen-items memory before running, so every matching paper "
        "surfaces again (demo control)",
    )
    args = parser.parse_args()

    if not PROFILE.get("specialties") and not args.specialty:
        print(
            "Warning: No specialties configured in context/physician.py.\n"
            "Add specialties or use --specialty <name> to override.\n"
        )

    asyncio.run(run_pipeline(args.days, args.specialty, args.dry_run, args.reset_seen))


if __name__ == "__main__":
    main()
