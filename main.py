"""
Funding opportunity pipeline for Ethiopian CSOs.

  Stage 0  maintenance  - delete expired/stale opportunities, skip stale queue items (server-side SQL)
  Stage 1  collect      - pull recent posts from each source's WordPress API into `raw_opportunities`
  Stage 2  process      - pre-filter without AI, then one AI call per remaining post; save relevant ones

Usage:
  python main.py                         # full run
  python main.py --stages collect        # only scrape
  python main.py --max-ai-calls 50       # smaller AI budget for this run
  python main.py --dry-run --limit 5     # scrape + analyse 5 posts, print results, write nothing
  python main.py --check                 # read-only check that the Supabase key works
"""
import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import config
import database_manager as db
from ai_analyzer import MalformedAIResponse, analyze
from filters import assess_relevance, is_clearly_expired, normalize_deadline
from llm import LLMClient, TemporaryAIError
from scrapers import collect_all

logger = logging.getLogger("pipeline")

STATUS_FLUSH_EVERY = 20


def setup_logging():
    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)
    root.handlers.clear()
    fmt = logging.Formatter(config.LOG_FORMAT)
    for handler in (logging.FileHandler(config.LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    for noisy in ("urllib3", "httpx", "httpcore", "google_genai", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _date_only(ts: str | None) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Decision logic for one opportunity (pure apart from the AI call; easy to test)
# ---------------------------------------------------------------------------
def evaluate(opp: dict, llm: LLMClient) -> tuple[str, dict | None]:
    """
    Returns (status, final_record_or_None).
    Raises TemporaryAIError when it should be retried on a later run.
    """
    title, text = opp.get("title") or "", opp.get("full_text") or ""
    published = _date_only(opp.get("published_at") or opp.get("scraped_at"))

    if len(text) < 200:
        logger.info("SKIP (no content): %s", title[:70])
        return "processed_irrelevant", None

    expired_on = is_clearly_expired(text)
    if expired_on:
        logger.info("SKIP (deadline %s passed, no AI used): %s", expired_on, title[:70])
        return "processed_expired", None

    try:
        result, model = analyze(llm, title, text, published.isoformat() if published else None)
    except MalformedAIResponse as e:
        logger.error("AI output unusable for '%s': %s", title[:70], e)
        return "processed_ai_error", None

    relevant, reason = assess_relevance(result["eligible_locations"], result["excluded_locations"],
                                        result["ethiopia_eligible"])
    if not relevant:
        logger.info("DROP (%s): %s", reason, title[:70])
        return "processed_irrelevant", None

    if config.DROP_INDIVIDUAL_ONLY and result["applicant_types"] == ["Individuals"]:
        logger.info("DROP (individuals only): %s", title[:70])
        return "processed_irrelevant", None

    deadline = normalize_deadline(result["deadline"], reference=published)
    if deadline and date.fromisoformat(deadline) < date.today():
        logger.info("DROP (deadline %s passed): %s", deadline, title[:70])
        return "processed_expired", None

    logger.info("KEEP (%s) deadline=%s: %s", reason, deadline or result["deadline"], title[:70])
    record = {
        "link": opp["link"],
        "title": title,
        "source": opp.get("source"),
        "geographic_scope": ", ".join(result["eligible_locations"]) or "Not specified",
        "funding_amount": result["funding_amount"],
        "funder": result["funder"],
        "deadline": deadline,
        "raw_deadline_text": result["deadline"],
        "focus_areas": ", ".join(result["focus_areas"]),
        "applicant_types": ", ".join(result["applicant_types"]),
        "summary": result["summary"],
        # Fall back to when we first saw it, so the age-based cleanup still works for old queue items.
        "published_at": opp.get("published_at") or opp.get("scraped_at"),
        "ai_model": model,
    }
    return "processed_relevant", record


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------
def run_maintenance():
    logger.info(">>> STAGE 0: maintenance")
    counts = db.run_maintenance(config.STALE_OPPORTUNITY_MONTHS, config.RAW_TEXT_RETENTION_DAYS,
                                config.ARTICLE_CUTOFF_MONTHS)
    logger.info("Maintenance: %s", counts)


def run_collector(dry_run: bool = False) -> list[dict]:
    logger.info(">>> STAGE 1: collect")
    existing = set() if dry_run else db.get_all_scraped_links()
    logger.info("%d links already known", len(existing))
    records = collect_all(existing)
    if not dry_run:
        db.add_raw_opportunities(records)
    logger.info("Collected %d new posts", len(records))
    return records


def run_processor(llm: LLMClient, max_ai_calls: int, items: list[dict] | None = None, dry_run: bool = False):
    logger.info(">>> STAGE 2: process")
    pending = items if items is not None else db.get_pending_opportunities()
    logger.info("%d posts pending analysis (AI budget this run: %d calls, %d min)",
                len(pending), max_ai_calls, config.MAX_PROCESSOR_MINUTES)
    if not pending:
        return

    deadline_ts = time.monotonic() + config.MAX_PROCESSOR_MINUTES * 60
    stats, buffer = {}, []
    gate = threading.Lock()
    in_flight = 0

    def worker(opp: dict):
        """Runs in a thread. Returns (status, record), or a ('left_pending', reason) marker."""
        nonlocal in_flight
        with gate:
            if llm.calls_made + in_flight >= max_ai_calls:
                return "left_pending", "AI call budget reached"
            if time.monotonic() > deadline_ts:
                return "left_pending", "time budget reached"
            if not llm.available:
                return "left_pending", "no AI provider left (quota exhausted?)"
            in_flight += 1
        try:
            return evaluate(opp, llm)
        except TemporaryAIError as e:
            return "left_pending", f"temporary AI failure: {e}"
        finally:
            with gate:
                in_flight -= 1

    def flush():
        if buffer and not dry_run:
            db.bulk_update_statuses(buffer)
        buffer.clear()

    # Worker threads only call the AI; all database writes happen here, on the main thread.
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, config.MAX_CONCURRENT_AI_CALLS)) as pool:
            futures = {pool.submit(worker, opp): opp for opp in pending}
            for future in as_completed(futures):
                opp, done = futures[future], done + 1
                try:
                    status, record = future.result()
                except Exception:
                    logger.exception("Unexpected error analysing %s; left pending", opp.get("link"))
                    status, record = "left_pending", "unexpected error"
                stats[status] = stats.get(status, 0) + 1
                if status == "left_pending":
                    logger.debug("Left pending (%s): %s", record, opp.get("link"))
                    continue
                if dry_run:
                    if record:
                        print(json.dumps(record, indent=2, ensure_ascii=False))
                    continue
                if record:
                    db.upsert_processed_opportunity(record)
                buffer.append({"link": opp["link"], "status": status})
                if len(buffer) >= STATUS_FLUSH_EVERY:
                    flush()
                if done % 25 == 0:
                    logger.info("Progress: %d/%d, %d AI calls, %s", done, len(pending), llm.calls_made, stats)
    finally:
        flush()
    if stats.get("left_pending"):
        logger.warning("%d posts left pending for the next run (budget, time limit or AI unavailable)",
                       stats["left_pending"])
    logger.info("Processor finished: %s; AI calls used: %d", stats, llm.calls_made)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stages", default="maintenance,collect,process",
                        help="comma-separated subset of: maintenance,collect,process")
    parser.add_argument("--max-ai-calls", type=int, default=config.MAX_AI_CALLS_PER_RUN)
    parser.add_argument("--dry-run", action="store_true", help="don't read/write the database; print results")
    parser.add_argument("--limit", type=int, default=0, help="dry-run only: analyse at most N posts")
    parser.add_argument("--check", action="store_true", help="read-only Supabase connection check, then exit")
    args = parser.parse_args(argv)
    stages = {s.strip() for s in args.stages.split(",")}

    setup_logging()
    if args.check:
        try:
            logger.info("Supabase connection OK: %s", db.check_connection())
            return 0
        except Exception:
            logger.critical("Supabase connection FAILED", exc_info=True)
            return 1
    started = time.time()
    logger.info("=== Funding pipeline run started (stages: %s%s) ===", ",".join(sorted(stages)),
                ", DRY RUN" if args.dry_run else "")
    failed = False
    try:
        if args.dry_run:
            records = run_collector(dry_run=True)
            if "process" in stages:
                records.sort(key=lambda r: r.get("published_at") or "", reverse=True)
                run_processor(LLMClient(), args.max_ai_calls, records[: args.limit or None], dry_run=True)
        else:
            if "maintenance" in stages:
                run_maintenance()
            if "collect" in stages:
                run_collector()
            if "process" in stages:
                llm = LLMClient()
                if llm.providers:
                    run_processor(llm, args.max_ai_calls)
                else:
                    failed = True
    except Exception:
        logger.critical("Pipeline crashed", exc_info=True)
        failed = True
    logger.info("=== Run finished in %.1f min%s ===", (time.time() - started) / 60, " WITH ERRORS" if failed else "")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
