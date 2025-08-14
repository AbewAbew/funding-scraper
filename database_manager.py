# database_manager.py (Corrected and Updated for Bulk Operations)
import logging
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from utils import init_supabase_client
from postgrest.exceptions import APIError

logger = logging.getLogger(__name__)

# Initialize the Supabase client once when the module is loaded
supabase = init_supabase_client()

def add_raw_opportunities(opportunities: list):
    """
    Adds a list of raw scraped opportunities to the 'raw_opportunities' table.
    Uses upsert to avoid errors on duplicate links, effectively performing an
    "INSERT IGNORE" operation.
    """
    if not supabase:
        logger.error("Supabase client not initialized. Cannot add raw opportunities.")
        return 0

    if not opportunities:
        logger.info("No raw opportunities to add.")
        return 0

    try:
        response = supabase.table('raw_opportunities').upsert(opportunities).execute()
        logger.info(f"Successfully sent {len(opportunities)} raw opportunities to the database for upsert.")
        return len(opportunities)
    except APIError as e:
        logger.error(f"Supabase API error while adding raw opportunities: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred while adding raw opportunities: {e}", exc_info=True)
    return 0

def get_all_scraped_links() -> set:
    """
    Retrieves a set of all links that have ever been scraped.
    """
    if not supabase:
        logger.error("Supabase client not initialized. Cannot fetch scraped links.")
        return set()

    all_links = set()
    try:
        page = 0
        page_size = 1000
        while True:
            response = supabase.table('raw_opportunities').select('link').range(page * page_size, (page + 1) * page_size - 1).execute()
            data = response.data
            if not data:
                break
            
            for item in data:
                all_links.add(item['link'])
            
            page += 1
            logger.debug(f"Fetched page {page} of links. Total links so far: {len(all_links)}")

        logger.info(f"Retrieved a total of {len(all_links)} existing links from the 'raw_opportunities' table.")
        return all_links
    except APIError as e:
        logger.error(f"Supabase API error while fetching all scraped links: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred while fetching links: {e}", exc_info=True)
    return all_links

def get_pending_opportunities() -> list:
    """
    Fetches all opportunities from 'raw_opportunities' that need AI analysis.
    """
    if not supabase:
        logger.error("Supabase client not initialized. Cannot fetch pending opportunities.")
        return []

    try:
        response = supabase.table('raw_opportunities').select('*', count='exact').eq('status', 'pending_analysis').execute()
        count = response.count
        logger.info(f"Found {count} opportunities pending analysis.")
        return response.data
    except APIError as e:
        logger.error(f"Supabase API error while fetching pending opportunities: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred fetching pending opportunities: {e}", exc_info=True)
    return []

# --- This function will no longer be called in the main loop, replaced by the bulk version ---
def update_raw_opportunity_status(link: str, status: str):
    """
    Updates the status of a SINGLE opportunity in the 'raw_opportunities' table.
    DEPRECATED for use in loops; use bulk_update_raw_opportunity_statuses instead.
    """
    if not supabase:
        logger.error("Supabase client not initialized. Cannot update status.")
        return

    try:
        supabase.table('raw_opportunities').update({'status': status}).eq('link', link).execute()
        logger.debug(f"Updated status for {link} to '{status}'.")
    except APIError as e:
        logger.error(f"Supabase API error updating status for {link}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred updating status for {link}: {e}", exc_info=True)

# --- START: NEW BULK UPDATE FUNCTION ---
def bulk_update_raw_opportunity_statuses(updates: list[dict]):
    """
    Updates the status of multiple opportunities in a single database call
    by calling the 'bulk_update_raw_status' stored procedure.

    Args:
        updates (list[dict]): A list of dictionaries, where each dict has
                              a 'link' and 'status' key.
                              e.g., [{'link': 'url1', 'status': 'processed_relevant'}, ...]
    """
    if not supabase:
        logger.error("Supabase client not initialized. Cannot perform bulk update.")
        return

    if not updates:
        logger.info("No status updates to perform in bulk.")
        return

    try:
        # Call the 'bulk_update_raw_status' stored procedure we created in Supabase.
        # The first argument is the function name, the second is a dictionary of parameters.
        supabase.rpc('bulk_update_raw_status', {'status_updates': updates}).execute()
        logger.info(f"Successfully sent a bulk update for {len(updates)} opportunity statuses.")
    except APIError as e:
        logger.error(f"Supabase API error during bulk status update: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred during bulk status update: {e}", exc_info=True)
# --- END: NEW BULK UPDATE FUNCTION ---

def add_processed_opportunity(opportunity_data: dict):
    """
    Adds a fully processed, relevant opportunity to the 'processed_opportunities' table.
    """
    if not supabase:
        logger.error("Supabase client not initialized. Cannot add processed opportunity.")
        return

    try:
        supabase.table('processed_opportunities').upsert(opportunity_data).execute()
        logger.info(f"Successfully saved processed opportunity '{opportunity_data.get('title', 'N/A')[:40]}...' to the database.")
    except APIError as e:
        logger.error(f"Supabase API error while adding processed opportunity: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred adding processed opportunity: {e}", exc_info=True)

def delete_expired_opportunities():
    """
    Deletes records from 'processed_opportunities' where the 'deadline' is in the past.
    """
    if not supabase:
        logger.error("Supabase client not initialized. Cannot delete expired opportunities.")
        return

    try:
        today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        logger.info(f"Checking for opportunities with deadlines before {today_utc}...")
        
        # --- FIX: Use `None` to check for SQL NULL instead of the string 'null' ---
        response = supabase.table('processed_opportunities').delete(count='exact').not_.is_('deadline', None).lt('deadline', today_utc).execute()
        
        count = response.count
        if count > 0:
            logger.info(f"Successfully deleted {count} expired opportunities.")
        else:
            logger.info("No expired opportunities found to delete.")

    except APIError as e:
        logger.error(f"Supabase API error while deleting expired opportunities: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred while deleting expired opportunities: {e}", exc_info=True)

def delete_stale_opportunities(threshold_months: int):
    """
    Deletes "stale" records: those with no deadline that are older than a
    given threshold.
    """
    if not supabase:
        logger.error("Supabase client not initialized. Cannot delete stale opportunities.")
        return

    try:
        cutoff_date = datetime.now(timezone.utc) - relativedelta(months=threshold_months)
        cutoff_iso = cutoff_date.isoformat()
        
        logger.info(f"Checking for stale opportunities created before {cutoff_date.strftime('%Y-%m-%d')}...")

        stale_keywords = ['Not Specified', 'N/A']

        # --- FIX: Use `None` to check for SQL NULL instead of the string 'null' ---
        response = (
            supabase.table('processed_opportunities')
            .delete(count='exact')
            .is_('deadline', None) # Condition 1: Must not have a parsed deadline.
            .in_('raw_deadline_text', stale_keywords) # Condition 2: Must be explicitly "Not Specified".
            .lt('processed_at', cutoff_iso) # Condition 3: Must be older than our threshold.
            .execute()
        )
        
        count = response.count
        if count > 0:
            logger.info(f"Successfully deleted {count} stale opportunities.")
        else:
            logger.info("No stale opportunities found to delete.")
            
    except APIError as e:
        logger.error(f"Supabase API error while deleting stale opportunities: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"An unexpected error occurred while deleting stale opportunities: {e}", exc_info=True)