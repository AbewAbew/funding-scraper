# main.py (Corrected and Refactored for Bulk Updates)
import time
import concurrent.futures
import threading
import logging
import re
from datetime import datetime, date
from dateutil.parser import parse as parse_date

import config
from gso_scraper import scrape_gso
from ofy_scraper import scrape_ofy
from od_scraper import scrape_od
from ai_analyzer import get_geographic_scope, get_enrichment_data
import database_manager as db

logger = logging.getLogger(__name__)

# --- Helper Functions ---

def setup_logging():
    """Configures the root logger based on settings in config.py."""
    root_logger = logging.getLogger()
    root_logger.setLevel(config.LOG_LEVEL)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    file_handler = logging.FileHandler(config.LOG_FILE, mode='a', encoding='utf-8')
    file_handler.setLevel(config.LOG_LEVEL)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(config.LOG_LEVEL)

    formatter = logging.Formatter(config.LOG_FORMAT)
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    logging.getLogger('google.api_core').setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)

def validate_and_clean_deadline(deadline_str: str) -> str | None:
    """
    Tries to parse a date string and returns it in YYYY-MM-DD format.
    Returns None if the string is a non-date keyword or cannot be interpreted as a date.
    """
    if not deadline_str or not isinstance(deadline_str, str):
        return None

    if any(keyword in deadline_str.lower() for keyword in ['rolling', 'ongoing', 'specified', 'quarterly', 'n/a']):
        logger.debug(f"Deadline '{deadline_str}' identified as a non-date keyword. Storing as NULL.")
        return None

    try:
        parsed_date = parse_date(deadline_str)
        return parsed_date.strftime('%Y-%m-%d')
    except (ValueError, TypeError):
        logger.debug(f"Could not parse '{deadline_str}' as a valid date. Storing as NULL.")
        return None

# --- Core Pipeline Stages ---

def run_maintenance_stage():
    """
    Stage 0: Performs database maintenance:
    1. Deletes opportunities with a hard deadline that is in the past.
    2. Deletes "stale" opportunities (no deadline and older than a set threshold).
    """
    logger.info("=========================================================")
    logger.info(">>> STAGE 0: MAINTENANCE - STARTING CLEANUP <<<")
    logger.info("=========================================================")
    db.delete_expired_opportunities()
    db.delete_stale_opportunities(config.STALE_OPPORTUNITY_MONTHS)
    logger.info(">>> MAINTENANCE STAGE COMPLETE <<<")

def run_collector_stage():
    """
    Stage 1: Scrapes all sources concurrently, filters out already-scraped links,
    and saves new raw opportunities to the database.
    """
    logger.info("=========================================================")
    logger.info(">>> STAGE 1: COLLECTOR - STARTING SCRAPING PHASE <<<")
    logger.info("=========================================================")
    existing_links = db.get_all_scraped_links()
    all_new_raw_opportunities = []
    scraper_functions = [scrape_gso, scrape_ofy, scrape_od]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(scraper_functions)) as executor:
        future_to_scraper = {executor.submit(f, existing_links): f.__name__ for f in scraper_functions}
        for future in concurrent.futures.as_completed(future_to_scraper):
            scraper_name = future_to_scraper[future]
            try:
                result = future.result()
                if result:
                    all_new_raw_opportunities.extend(result)
            except Exception:
                logger.error(f"Scraper '{scraper_name}' failed during execution.", exc_info=True)

    logger.info(f"Scraping complete. Found a total of {len(all_new_raw_opportunities)} new raw opportunities across all sources.")
    unique_opportunities = []
    seen_links = set()
    for opp in all_new_raw_opportunities:
        link = opp.get('link')
        if link and link not in seen_links:
            unique_opportunities.append(opp)
            seen_links.add(link)
    if len(all_new_raw_opportunities) > len(unique_opportunities):
        removed_count = len(all_new_raw_opportunities) - len(unique_opportunities)
        logger.info(f"De-duplicated raw opportunities: removed {removed_count} cross-posted duplicates.")
    if unique_opportunities:
        db.add_raw_opportunities(unique_opportunities)
    else:
        logger.info("No new raw opportunities found to save.")
    logger.info(">>> COLLECTOR STAGE COMPLETE <<<")


# --- START: REFACTORED PROCESSOR STAGE ---
def run_processor_stage():
    """
    Stage 2: Fetches 'pending' opportunities, runs them through the AI pipeline,
    and updates their statuses in a single efficient batch operation at the end.
    """
    logger.info("=========================================================")
    logger.info(">>> STAGE 2: PROCESSOR - STARTING ANALYSIS PHASE <<<")
    logger.info("=========================================================")

    opportunities_to_process = db.get_pending_opportunities()
    if not opportunities_to_process:
        logger.info("No new opportunities are pending analysis. Processor stage finished.")
        return

    logger.info(f"Found {len(opportunities_to_process)} opportunities to analyze.")
    logger.info(f"AI analysis will run with max {config.MAX_CONCURRENT_AI_CALLS} concurrent calls.")

    ai_semaphore = threading.Semaphore(config.MAX_CONCURRENT_AI_CALLS)
    processed_count = 0
    relevant_count = 0
    
    # This list will collect all the status updates we need to make.
    status_updates_to_commit = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_AI_CALLS) as executor:
        # Submit the new worker function 'analyze_opportunity'.
        future_to_opp = {
            executor.submit(analyze_opportunity, opp, ai_semaphore): opp
            for opp in opportunities_to_process
        }

        for future in concurrent.futures.as_completed(future_to_opp):
            processed_count += 1
            try:
                # The worker now returns a tuple: (is_relevant, status_update_dict)
                is_relevant, status_update = future.result()
                
                # Only add to the batch if the analysis was successful
                if status_update:
                    status_updates_to_commit.append(status_update)
                
                if is_relevant:
                    relevant_count += 1
                
                logger.debug(f"Completed analysis for item {processed_count}/{len(opportunities_to_process)}")

            except Exception as e:
                opp = future_to_opp[future]
                logger.error(f"A critical unhandled error occurred while analyzing {opp.get('link', 'N/A')}", exc_info=True)

    # After the loop finishes, perform the single bulk update.
    if status_updates_to_commit:
        logger.info(f"Committing {len(status_updates_to_commit)} status updates to the database in a single batch...")
        db.bulk_update_raw_opportunity_statuses(status_updates_to_commit)

    logger.info(f"Analysis complete. Of {processed_count} processed opportunities, {relevant_count} were relevant.")
    logger.info(">>> PROCESSOR STAGE COMPLETE <<<")


def analyze_opportunity(opportunity: dict, semaphore: threading.Semaphore) -> tuple[bool | None, dict | None]:
    """
    Analyzes a single opportunity and returns its relevance and the required status update.
    IT DOES NOT WRITE THE STATUS UPDATE TO THE DATABASE ITSELF.
    Returns: A tuple (is_relevant, status_update_dict).
             The status_update_dict is None if the item should be retried.
    """
    with semaphore:
        title = opportunity.get('title', 'No Title')
        link = opportunity.get('link')
        full_text = opportunity.get('full_text', '')

        logger.info(f"--- Analyzing: \"{title[:60]}...\" ---")

        geo_data = get_geographic_scope(title, full_text)
        is_relevant, reason = is_relevant_for_ethiopia(geo_data)

        if not is_relevant:
            logger.warning(f"Record DISCARDED (Reason: {reason}). Title: \"{title[:60]}...\"")
            return False, {'link': link, 'status': 'processed_irrelevant'}

        logger.info(f"Record KEPT (Reason: {reason}). Proceeding to full enrichment.")
        enrichment_data = get_enrichment_data(title, full_text)

        summary = enrichment_data.get('summary', '')
        funding = enrichment_data.get('funding_amount', '')

        if "AI call failed" in summary:
            logger.error(f"AI Enrichment FAILED (Temporary): '{title[:60]}...'. Will retry on next run.")
            return None, None
        elif funding == "Error" or "malformed JSON" in summary or "No JSON object" in summary:
            logger.error(f"AI Enrichment FAILED (Permanent: {summary}): '{title[:60]}...'. Discarding record.")
            return False, {'link': link, 'status': 'processed_ai_error'}

        raw_deadline_text = enrichment_data.get('deadline')
        parsed_deadline_str = validate_and_clean_deadline(raw_deadline_text)

        if parsed_deadline_str:
            deadline_date = datetime.strptime(parsed_deadline_str, '%Y-%m-%d').date()
            if deadline_date < date.today():
                logger.warning(f"Record DISCARDED (Reason: Deadline {parsed_deadline_str} is in the past). Title: \"{title[:60]}...\"")
                return False, {'link': link, 'status': 'processed_expired'}

        final_record = {
            'link': link,
            'title': title,
            'source': opportunity.get('source'),
            'geographic_scope': ", ".join(geo_data.get('eligible', [])),
            'funding_amount': enrichment_data.get('funding_amount'),
            'funder': enrichment_data.get('funder'),
            'deadline': parsed_deadline_str,
            'raw_deadline_text': raw_deadline_text,
            'focus_areas': ", ".join(enrichment_data.get('focus_areas', [])),
            'summary': enrichment_data.get('summary')
        }

        # This part is fine to do one-by-one as it's an INSERT, not a high-frequency UPDATE.
        db.add_processed_opportunity(final_record)
        
        # Return the final status update to be processed in the batch.
        return True, {'link': link, 'status': 'processed_relevant'}
# --- END: REFACTORED PROCESSOR STAGE ---


def is_relevant_for_ethiopia(geo_data: dict) -> tuple[bool, str]:
    """
    Applies strict, deterministic rules to decide if an opportunity is relevant.
    """
    eligible = [str(g).lower().strip() for g in geo_data.get('eligible', [])]
    excluded = [str(g).lower().strip() for g in geo_data.get('excluded', [])]

    general_scopes = {
        'east africa', 'horn of africa', 'africa', 'sub-saharan africa',
        'global', 'international', 'developing countries'
    }

    if 'ethiopia' in excluded:
        return False, "Explicitly excludes Ethiopia"
    if 'ethiopia' in eligible:
        return True, "Explicitly includes Ethiopia"
    
    specific_countries_found = [loc for loc in eligible if loc not in general_scopes]
    if specific_countries_found:
        return False, f"Specific country list found which does not include Ethiopia: {specific_countries_found}"
    
    acceptable_general_scopes = [scope for scope in eligible if scope in general_scopes]
    if acceptable_general_scopes:
        return True, f"Includes a relevant general scope: {acceptable_general_scopes}"

    return False, f"No relevant geographic scope found. Raw eligible: {eligible}"


# --- Main Script Execution ---

if __name__ == "__main__":
    setup_logging()
    start_time = time.time()
    logger.info("=========================================================")
    logger.info("=== STARTING NEW FUNDING AGGREGATOR RUN (Pipeline v7 - Bulk Operations) ===")
    
    try:
        run_maintenance_stage()
        run_collector_stage()
        run_processor_stage()
    except Exception as e:
        logger.critical("A critical unhandled exception occurred in the main pipeline. Shutting down.", exc_info=True)

    end_time = time.time()
    logger.info("=========================================================")
    logger.info(f"Total process took {((end_time - start_time) / 60):.2f} minutes.")
    logger.info("=== RUN FINISHED ===")
    logger.info("=========================================================\n")