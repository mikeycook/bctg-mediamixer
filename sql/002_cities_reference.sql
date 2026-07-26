-- Read-only copy of the cities list from the golden source on server 2.
-- Run as the bigcity owner. Idempotent.
--
-- Deliberately NOT a foreign key target for content_library_assets.cityid.
-- This table lags: a city added on server 2 and not yet copied here would
-- make a legitimate asset fail to insert. Rights and review fail closed; a
-- replicated lookup fails soft, and the sync reports cityid values that do
-- not resolve rather than rejecting them.
--
-- Refresh with migration/ExportFromServer2.sh, which reads server 2 and
-- never writes to it.

BEGIN;

CREATE TABLE IF NOT EXISTS public.cities_reference (
    cityid       TEXT        PRIMARY KEY,
    country      TEXT,
    city_slug    TEXT,
    refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cities_reference_slug_idx
    ON public.cities_reference (city_slug);

ALTER TABLE public.cities_reference OWNER TO bigcity;

COMMIT;
