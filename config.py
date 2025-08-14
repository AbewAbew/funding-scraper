import logging

# --- General Configuration ---
# This is no longer used, as we've moved to a cloud database.
# DATABASE_FILE = 'funding_opportunities.db'

# --- AI Configuration ---
MAX_CONCURRENT_AI_CALLS = 5

VALID_FOCUS_AREAS = [
    "Human Rights", "Education", "Health", "Youth Empowerment", "Women & Girls",
    "Climate & Environment", "Agriculture & Food Security", "Economic Development",
    "Technology & Innovation", "Peace & Conflict Resolution", "Water & Sanitation",
    "Arts & Culture", "Democracy & Governance", "Disability Inclusion",
    "Humanitarian Aid", "Research"
]

# --- Scraper Configuration ---
SCRAPER_TEST_LIMIT = 0
ARTICLE_CUTOFF_MONTHS = 12

# --- NEW: Maintenance Configuration ---
# The number of months after which an opportunity with no specified deadline
# is considered "stale" and can be deleted.
STALE_OPPORTUNITY_MONTHS = 9


# --- Logging Configuration ---
LOG_LEVEL = logging.DEBUG
LOG_FILE = 'funding_scraper.log'
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'