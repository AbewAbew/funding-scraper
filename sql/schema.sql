-- =====================================================================
-- Funding scraper database schema (November 2026)
--
-- Safe to run on BOTH a brand-new Supabase project and the existing one:
-- every statement is idempotent (IF NOT EXISTS / CREATE OR REPLACE) and
-- nothing here drops tables or deletes data.
--
-- Run it once in the Supabase SQL Editor before the first pipeline run.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Raw queue: every post ever collected (also the "already seen" cache)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.raw_opportunities (
    link        TEXT PRIMARY KEY,
    title       TEXT,
    source      TEXT,
    full_text   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending_analysis',
    scraped_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Added in the 2026 rewrite
ALTER TABLE public.raw_opportunities ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;

COMMENT ON COLUMN public.raw_opportunities.status IS
  'pending_analysis | processed_relevant | processed_irrelevant | processed_expired | processed_ai_error | skipped_stale';

CREATE INDEX IF NOT EXISTS raw_opportunities_status_published_idx
    ON public.raw_opportunities (status, published_at DESC);

-- ---------------------------------------------------------------------
-- 2. Final table: relevant, enriched opportunities (what users see)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.processed_opportunities (
    link              TEXT PRIMARY KEY,
    title             TEXT,
    source            TEXT,
    geographic_scope  TEXT,
    funding_amount    TEXT,
    funder            TEXT,
    deadline          DATE,           -- NULL when rolling / not specified
    focus_areas       TEXT,           -- comma-separated, e.g. 'Health, Education'
    summary           TEXT,
    processed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE public.processed_opportunities ADD COLUMN IF NOT EXISTS raw_deadline_text TEXT;
-- Added in the 2026 rewrite
ALTER TABLE public.processed_opportunities ADD COLUMN IF NOT EXISTS applicant_types TEXT;  -- e.g. 'Organizations, Researchers'
ALTER TABLE public.processed_opportunities ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE public.processed_opportunities ADD COLUMN IF NOT EXISTS ai_model TEXT;

CREATE INDEX IF NOT EXISTS processed_opportunities_deadline_idx
    ON public.processed_opportunities (deadline);

-- ---------------------------------------------------------------------
-- 3. Row Level Security
--    The pipeline uses the service_role key, which bypasses RLS.
--    Anyone using the public anon key (e.g. a website) can only READ
--    processed_opportunities and cannot touch the raw queue.
-- ---------------------------------------------------------------------
ALTER TABLE public.raw_opportunities ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.processed_opportunities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read access" ON public.processed_opportunities;
CREATE POLICY "Public read access" ON public.processed_opportunities
    FOR SELECT TO anon, authenticated USING (true);

-- ---------------------------------------------------------------------
-- 4. Bulk status update (called by the pipeline)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.bulk_update_raw_status(status_updates jsonb)
RETURNS void
LANGUAGE sql
SET search_path = public
AS $$
  UPDATE public.raw_opportunities r
     SET status = u.status
    FROM (
      SELECT value ->> 'link' AS link, value ->> 'status' AS status
        FROM jsonb_array_elements(status_updates)
    ) u
   WHERE r.link = u.link;
$$;

-- ---------------------------------------------------------------------
-- 5. Maintenance (called at the start of every run)
--    * delete opportunities whose deadline has passed
--    * delete opportunities with no deadline older than `stale_months` (rolling ones: 2x)
--    * mark very old, never-analysed queue items as skipped_stale (saves AI calls)
--    * clear stored article text of handled items after `raw_text_days`
--      (the link stays, so the post is never scraped/analysed twice)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.run_maintenance(
    stale_months          int DEFAULT 6,
    raw_text_days         int DEFAULT 60,
    pending_cutoff_months int DEFAULT 6
)
RETURNS jsonb
LANGUAGE plpgsql
SET search_path = public
AS $$
DECLARE
    n_expired int; n_stale int; n_skipped int; n_trimmed int;
BEGIN
    DELETE FROM processed_opportunities
     WHERE deadline IS NOT NULL AND deadline < CURRENT_DATE;
    GET DIAGNOSTICS n_expired = ROW_COUNT;

    -- No stated deadline: drop after stale_months. Explicitly rolling (open all year): allow twice as long.
    DELETE FROM processed_opportunities
     WHERE deadline IS NULL
       AND COALESCE(published_at, processed_at) < NOW() - make_interval(
             months => CASE WHEN raw_deadline_text ILIKE '%rolling%' THEN stale_months * 2 ELSE stale_months END);
    GET DIAGNOSTICS n_stale = ROW_COUNT;

    UPDATE raw_opportunities SET status = 'skipped_stale'
     WHERE status = 'pending_analysis'
       AND COALESCE(published_at, scraped_at) < NOW() - make_interval(months => pending_cutoff_months);
    GET DIAGNOSTICS n_skipped = ROW_COUNT;

    UPDATE raw_opportunities SET full_text = NULL
     WHERE full_text IS NOT NULL
       AND status <> 'pending_analysis'
       AND scraped_at < NOW() - make_interval(days => raw_text_days);
    GET DIAGNOSTICS n_trimmed = ROW_COUNT;

    RETURN jsonb_build_object(
        'deleted_expired', n_expired,
        'deleted_stale', n_stale,
        'queue_skipped_stale', n_skipped,
        'raw_text_cleared', n_trimmed
    );
END;
$$;

-- Only the pipeline (service_role) may call these functions; the public anon key may not.
REVOKE EXECUTE ON FUNCTION public.bulk_update_raw_status(jsonb) FROM PUBLIC, anon, authenticated;
REVOKE EXECUTE ON FUNCTION public.run_maintenance(int, int, int) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.bulk_update_raw_status(jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.run_maintenance(int, int, int) TO service_role;
