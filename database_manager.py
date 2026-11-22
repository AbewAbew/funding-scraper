"""All Supabase access. Requires the tables and functions from sql/schema.sql."""
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache

from postgrest.types import ReturnMethod

import config  # noqa: F401  (loads .env)

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000  # PostgREST's default max rows per request


class DatabaseUnavailable(RuntimeError):
    pass


def resolve_backend_key() -> str | None:
    """
    Prefer the new secret key (sb_secret_...). Fall back to the legacy service_role JWT,
    which Supabase deprecates at the end of 2026, so both work during the migration.
    """
    key = os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not key:
        return None
    key = key.strip()
    if key.startswith("sb_publishable_"):
        raise DatabaseUnavailable("A publishable key was supplied; the pipeline needs the secret key (sb_secret_...)")
    if key.startswith("sb_secret_"):
        logger.info("Supabase: using secret API key")
    else:
        logger.warning("Supabase: using the legacy service_role key (deprecated end of 2026). "
                       "Set SUPABASE_SECRET_KEY to an sb_secret_... key.")
    return key


def resolve_url() -> str | None:
    """Accept the project URL with or without the '/rest/v1' suffix the dashboard sometimes shows."""
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    for suffix in ("/rest/v1", "/rest"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url or None


@lru_cache(maxsize=1)
def client():
    url, key = resolve_url(), resolve_backend_key()
    if not (url and key):
        raise DatabaseUnavailable("SUPABASE_URL and SUPABASE_SECRET_KEY (or legacy SUPABASE_SERVICE_KEY) must be set")
    from supabase import create_client

    return create_client(url, key)


def _paginate(build_query) -> list[dict]:
    rows, start = [], 0
    while True:
        batch = build_query().range(start, start + PAGE_SIZE - 1).execute().data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        start += PAGE_SIZE


def check_connection() -> dict:
    """Read-only connectivity check: counts rows in both tables."""
    counts = {}
    for table in ("raw_opportunities", "processed_opportunities"):
        counts[table] = client().table(table).select("link", count="exact").limit(1).execute().count
    return counts


# --- Maintenance -----------------------------------------------------------
def run_maintenance(stale_months: int, raw_text_days: int, article_cutoff_months: int) -> dict:
    """Server-side cleanup (see sql function `run_maintenance`). Returns counts per action."""
    result = client().rpc("run_maintenance", {
        "stale_months": stale_months,
        "raw_text_days": raw_text_days,
        "pending_cutoff_months": article_cutoff_months,
    }).execute()
    return result.data or {}


# --- Raw queue -------------------------------------------------------------
def get_all_scraped_links() -> set[str]:
    rows = _paginate(lambda: client().table("raw_opportunities").select("link").order("link"))
    return {r["link"] for r in rows}


def add_raw_opportunities(records: list[dict]) -> int:
    if not records:
        return 0
    for i in range(0, len(records), 200):
        chunk = records[i:i + 200]
        client().table("raw_opportunities").upsert(
            chunk, on_conflict="link", ignore_duplicates=True, returning=ReturnMethod.minimal
        ).execute()
    return len(records)


def get_pending_opportunities() -> list[dict]:
    """Newest first, so if a run hits its AI budget the freshest opportunities were handled."""
    return _paginate(lambda: client().table("raw_opportunities")
                     .select("link,title,source,full_text,published_at,scraped_at")
                     .eq("status", "pending_analysis")
                     .order("published_at", desc=True, nullsfirst=False)
                     .order("link"))


def bulk_update_statuses(updates: list[dict]):
    """updates: [{'link': ..., 'status': ...}, ...]"""
    if updates:
        client().rpc("bulk_update_raw_status", {"status_updates": updates}).execute()


# --- Final table -----------------------------------------------------------
def upsert_processed_opportunity(record: dict):
    record = {**record, "processed_at": datetime.now(timezone.utc).isoformat()}
    client().table("processed_opportunities").upsert(record, on_conflict="link").execute()
