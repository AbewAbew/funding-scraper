"""Central configuration. Secrets come from environment variables (.env locally, GitHub Secrets in CI)."""
import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --- Sources (WordPress REST API: /wp-json/wp/v2/posts?categories=<id>) ---
# `proxy_in_ci`: route through Tor when running in GitHub Actions (site blocks datacenter IPs).
SOURCES = [
    {"name": "Global South Opportunities", "base_url": "https://www.globalsouthopportunities.com", "category_id": 26},
    {"name": "Opportunities For Youth", "base_url": "https://opportunitiesforyouth.org", "category_id": 5},
    {"name": "Opportunity Desk", "base_url": "https://opportunitydesk.org", "category_id": 30, "proxy_in_ci": True},
]
TOR_PROXY = "socks5h://127.0.0.1:9050"

# Only collect posts published within this window. Most grants close within a few months of being posted.
ARTICLE_CUTOFF_MONTHS = _env_int("ARTICLE_CUTOFF_MONTHS", 6)

# --- AI ---
# Providers are tried in this order; ones without an API key are skipped.
# Limits are client-side pacing so we stay inside free tiers. Raise them if AI Studio / Groq show higher limits.
AI_PROVIDERS = [
    {
        "name": "gemini",
        "kind": "gemini",
        "api_key_env": "GOOGLE_API_KEY",
        "model": os.getenv("GEMINI_MODEL", "gemma-4-31b-it"),
        "rpm": _env_int("GEMINI_RPM", 25),
        "tpm": _env_int("GEMINI_TPM", 15000),
    },
    {
        "name": "groq",
        "kind": "openai_compatible",
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
        "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        "rpm": _env_int("GROQ_RPM", 25),
        "tpm": _env_int("GROQ_TPM", 8000),
        "extra_body": {"reasoning_effort": "low"},
    },
    {
        "name": "openrouter",
        "kind": "openai_compatible",
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "model": os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free"),
        "rpm": _env_int("OPENROUTER_RPM", 15),
        "tpm": _env_int("OPENROUTER_TPM", 100000),
    },
]

# Hard caps per run, so the first (large) backlog is spread over several daily runs.
# Whatever isn't processed stays `pending_analysis` and is picked up next run (newest first).
MAX_AI_CALLS_PER_RUN = _env_int("MAX_AI_CALLS_PER_RUN", 400)
MAX_PROCESSOR_MINUTES = _env_int("MAX_PROCESSOR_MINUTES", 100)
# Posts analysed in parallel. Gemma 4 31B takes ~50s per call, so a few in flight keep us near the
# free-tier pace; the per-provider rate limiter still caps requests and tokens per minute.
MAX_CONCURRENT_AI_CALLS = _env_int("MAX_CONCURRENT_AI_CALLS", 4)

# How much article text the AI sees. ~4 chars per token.
AI_TEXT_CHAR_LIMIT = _env_int("AI_TEXT_CHAR_LIMIT", 9000)

# If True, opportunities that only individuals can apply for (scholarships, fellowships...) are dropped.
# Default False: they're kept and tagged in `applicant_types` so the front end can filter.
DROP_INDIVIDUAL_ONLY = os.getenv("DROP_INDIVIDUAL_ONLY", "false").lower() == "true"

VALID_FOCUS_AREAS = [
    "Human Rights", "Education", "Health", "Youth Empowerment", "Women & Girls",
    "Climate & Environment", "Agriculture & Food Security", "Economic Development",
    "Technology & Innovation", "Peace & Conflict Resolution", "Water & Sanitation",
    "Arts & Culture", "Democracy & Governance", "Disability Inclusion",
    "Humanitarian Aid", "Research",
]
VALID_APPLICANT_TYPES = ["Organizations", "Individuals", "Businesses", "Researchers", "Government"]

# --- Maintenance ---
# Opportunities without a parseable deadline are deleted this long after they were published/processed.
STALE_OPPORTUNITY_MONTHS = _env_int("STALE_OPPORTUNITY_MONTHS", 6)
# Raw article text of already-handled items is cleared after this many days (keeps the free DB small).
RAW_TEXT_RETENTION_DAYS = _env_int("RAW_TEXT_RETENTION_DAYS", 60)

# --- Logging ---
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
LOG_FILE = "funding_scraper.log"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
