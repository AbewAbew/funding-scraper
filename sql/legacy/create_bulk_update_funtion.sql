-- This function accepts a JSON array of objects.
-- Each object must have a "link" and a "status" key.
-- Example input: '[{"link": "http://a.com", "status": "processed_relevant"}, {"link": "http://b.com", "status": "processed_irrelevant"}]'

CREATE OR REPLACE FUNCTION bulk_update_raw_status(status_updates jsonb)
RETURNS void -- This function doesn't return any data
LANGUAGE plpgsql
AS $$
BEGIN
  -- This statement takes the JSON array, unnests it into a temporary table-like structure,
  -- and then joins it with the main raw_opportunities table on the 'link' column to perform the update.
  UPDATE
    public.raw_opportunities
  SET
    status = updates.status
  FROM (
    SELECT
      (value ->> 'link') AS link,
      (value ->> 'status') AS status
    FROM
      jsonb_array_elements(status_updates)
  ) AS updates
  WHERE
    public.raw_opportunities.link = updates.link;
END;
$$;
