# fix_focus_areas_format.py
import logging
from database_manager import supabase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def fix_focus_areas_format():
    """Convert existing focus areas from array format to comma-separated string."""
    if not supabase:
        logger.error("Supabase client not initialized.")
        return
    
    try:
        response = supabase.table('processed_opportunities').select('*').execute()
        opportunities = response.data
        
        logger.info(f"Found {len(opportunities)} opportunities to process")
        
        for i, opp in enumerate(opportunities):
            focus_areas = opp.get('focus_areas')
            if focus_areas and isinstance(focus_areas, list):
                focus_areas_str = ', '.join(focus_areas)
                supabase.table('processed_opportunities').update({
                    'focus_areas': focus_areas_str
                }).eq('link', opp['link']).execute()
                logger.info(f"Updated: {focus_areas} -> {focus_areas_str}")
                
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == "__main__":
    fix_focus_areas_format()