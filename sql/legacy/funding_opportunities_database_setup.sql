-- =================================================================
-- == FINAL, CORRECTED SCRIPT FOR DATABASE SETUP (Version 2)      ==
-- =================================================================

-- Drop tables if they exist, to ensure a clean start
DROP TABLE IF EXISTS public.raw_opportunities;
DROP TABLE IF EXISTS public.processed_opportunities;

-- Create the table for raw, unprocessed data
CREATE TABLE public.raw_opportunities (
    link TEXT PRIMARY KEY NOT NULL,
    title TEXT,
    source TEXT,
    full_text TEXT,
    status TEXT DEFAULT 'pending_analysis' NOT NULL, -- Values: pending_analysis, processed_relevant, processed_irrelevant
    scraped_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- Create the table for final, enriched, and relevant opportunities
CREATE TABLE public.processed_opportunities (
    link TEXT PRIMARY KEY NOT NULL,
    title TEXT,
    source TEXT,
    geographic_scope TEXT,
    funding_amount TEXT,
    funder TEXT,
    -- CORRECTED: Changed from TEXT to DATE for proper date comparison
    deadline DATE, -- This can be NULL for 'Rolling' or 'Not Specified'
    focus_areas TEXT,
    summary TEXT,
    processed_at TIMESTAMPTZ DEFAULT NOW() NOT NULL
);

-- Add comments for clarity
COMMENT ON TABLE public.raw_opportunities IS 'Stores all scraped opportunities before they are analyzed by AI.';
COMMENT ON TABLE public.processed_opportunities IS 'Stores final, enriched data for opportunities deemed relevant. Deadline is a proper DATE type.';
