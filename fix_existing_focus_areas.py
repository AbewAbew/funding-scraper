# fix_existing_focus_areas.py
import logging
from database_manager import supabase
from ai_analyzer import get_enrichment_data
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def fix_existing_focus_areas():
    """Re-process existing opportunities to fix overpopulated focus areas."""
    if not supabase:
        logger.error("Supabase client not initialized.")
        return
    
    try:
        # Get all processed opportunities
        response = supabase.table('processed_opportunities').select('*').execute()
        opportunities = response.data
        
        logger.info(f"Found {len(opportunities)} opportunities to process")
        
        for i, opp in enumerate(opportunities):
            logger.info(f"Processing {i+1}/{len(opportunities)}: {opp['title'][:50]}...")
            
            # Get new focus areas using AI
            enrichment = get_enrichment_data(opp['title'], opp.get('summary', ''))
            new_focus_areas = enrichment.get('focus_areas', [])
            
            # Update only if we got valid focus areas
            if new_focus_areas:
                supabase.table('processed_opportunities').update({
                    'focus_areas': new_focus_areas
                }).eq('link', opp['link']).execute()
                
                logger.info(f"Updated focus areas for '{opp['title'][:30]}...': {new_focus_areas}")
            
            # Rate limiting
            time.sleep(2)
            
    except Exception as e:
        logger.error(f"Error fixing focus areas: {e}")

if __name__ == "__main__":
    fix_existing_focus_areas()