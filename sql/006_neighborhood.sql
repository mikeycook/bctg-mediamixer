-- Run as the bigcity owner on the mediamixer database (server 3).
-- Idempotent. Adds a neighborhood to source clips so feature videos can be
-- scoped below the city — e.g. exteriors in Chelsea, not just in New York.
--
-- A plain TEXT column, like country: a clip sits in one neighborhood, and a
-- canonical neighborhoods table would be more machinery than the data earns
-- yet. Selection matches it case-insensitively, so 'Chelsea' and 'chelsea'
-- are the same place.

ALTER TABLE public.content_library_assets
    ADD COLUMN IF NOT EXISTS neighborhood TEXT;

CREATE INDEX IF NOT EXISTS content_library_assets_neighborhood_idx
    ON public.content_library_assets (lower(neighborhood))
    WHERE neighborhood IS NOT NULL;
