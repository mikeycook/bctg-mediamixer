-- Separates the path-derived city slug from the canonical city identifier.
-- Run as the bigcity owner. Idempotent.
--
-- The golden source keys cities as 'CIT-00000000002' with a display name in
-- cities.cityname. S3 paths carry a slug: ugc-assets/app/new-york/...
-- Writing the slug into cityid would put two incompatible identifier
-- formats in one column and quietly break every join back to cities.
--
-- So: city_slug holds the path proposal, cityid holds the resolved CIT- id,
-- and resolution matches city_slug against slugify(cityname). Unresolved
-- slugs are reported rather than rejected, because cities_reference is a
-- lagging copy of a database this server cannot reach.

BEGIN;

ALTER TABLE public.content_library_assets
    ADD COLUMN IF NOT EXISTS city_slug TEXT;

CREATE INDEX IF NOT EXISTS content_library_assets_city_slug_idx
    ON public.content_library_assets (city_slug)
    WHERE city_slug IS NOT NULL;

-- cityname is what the slug is derived from, so the reference copy needs it.
ALTER TABLE public.cities_reference
    ADD COLUMN IF NOT EXISTS cityname TEXT;

COMMIT;
