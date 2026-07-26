-- MediaMixer content library schema, applied to the mediamixer database on
-- server 3. Run as the bigcity owner. Idempotent — safe to run regardless of
-- what was applied before.
--
-- This is a fresh CREATE, not a migration. The equivalent table on server 2
-- is never altered; its rows are copied here once (see migration/) and the
-- original is left in place as frozen history.
--
-- Because the target is empty, CHECK constraints are created VALID rather
-- than NOT VALID. Legacy columns (type, duration, hook_compatibility) are
-- carried so the existing admin Content Library tab keeps working unchanged.

BEGIN;

-- ---------------------------------------------------------------------------
-- updated_at trigger function
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- Source assets
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.content_library_assets (
    id                    BIGSERIAL   PRIMARY KEY,

    -- Identity
    asset_id              TEXT        UNIQUE,          -- assigned, e.g. UGC-00001
    bucket_name           TEXT        NOT NULL DEFAULT 'big-city-travel-guide-clips',
    s3_key                TEXT        NOT NULL UNIQUE, -- e.g. ugc-assets/nyc/clip.mov
    s3_version_id         TEXT,
    filename              TEXT,
    folder                TEXT,                        -- e.g. ugc-assets/b-roll/food/pizza/

    -- Object facts, from S3
    size_bytes            BIGINT,
    content_type          TEXT,
    etag                  TEXT,
    s3_last_modified_at   TIMESTAMPTZ,
    checksum_sha256       TEXT,
    duplicate_of_asset_id BIGINT      REFERENCES public.content_library_assets(id)
                                      ON DELETE RESTRICT,

    -- Media facts, measured by ffprobe — never entered by hand
    duration_ms           BIGINT,
    width                 INTEGER,
    height                INTEGER,
    frame_rate            NUMERIC(10,4),
    video_codec           TEXT,
    audio_codec           TEXT,
    has_audio             BOOLEAN,
    orientation           TEXT,
    probe_data            JSONB,
    probe_error           TEXT,

    -- Semantics
    asset_type            TEXT,                        -- controlled: app|broll|reaction|...
    type                  TEXT,                        -- legacy free text, e.g. B-Roll
    subtype               TEXT,                        -- e.g. Restaurant Interior
    category              TEXT,                        -- e.g. Food
    subcategory           TEXT,                        -- e.g. Pizza
    place_name            TEXT,                        -- name of the place featured
    cityid                TEXT,                        -- soft reference to cities_reference
    country               TEXT,
    country_code          CHAR(2),
    notes                 TEXT,                        -- free-text detail, supplied to the AI

    -- Editorial
    shot_type             TEXT,
    camera_motion         TEXT,
    time_of_day           TEXT,
    people_present        BOOLEAN,
    speech_present        BOOLEAN,
    quality_score         SMALLINT,
    city_agnostic         BOOLEAN     NOT NULL DEFAULT false,
    hook_compatibility    TEXT[],                      -- legacy: {"Best Pizza","Hidden Gems"}
    duration              TEXT,                        -- legacy display form, e.g. 0:45

    -- Governance
    status                TEXT        NOT NULL DEFAULT 'discovered',
    rights_status         TEXT        NOT NULL DEFAULT 'unknown',
    rights_source         TEXT,
    rights_expires_at     TIMESTAMPTZ,
    reviewed_by           TEXT,
    reviewed_at           TIMESTAMPTZ,

    -- Operations
    first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    missing_since         TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT content_library_assets_duration_ms_check
        CHECK (duration_ms IS NULL OR duration_ms > 0),
    CONSTRAINT content_library_assets_dimensions_check
        CHECK ((width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)),
    CONSTRAINT content_library_assets_orientation_check
        CHECK (orientation IS NULL OR orientation IN ('portrait','landscape','square')),
    CONSTRAINT content_library_assets_quality_score_check
        CHECK (quality_score IS NULL OR quality_score BETWEEN 1 AND 5),
    CONSTRAINT content_library_assets_asset_type_check
        CHECK (asset_type IS NULL OR asset_type IN
            ('app','broll','reaction','music','voiceover','caption','cta')),
    CONSTRAINT content_library_assets_status_check
        CHECK (status IN
            ('discovered','probing','needs_review','active','rejected','missing','error','archived')),
    CONSTRAINT content_library_assets_rights_status_check
        CHECK (rights_status IN ('unknown','owned','licensed','restricted','expired')),
    CONSTRAINT content_library_assets_not_own_duplicate
        CHECK (duplicate_of_asset_id IS NULL OR duplicate_of_asset_id <> id)
);

CREATE UNIQUE INDEX IF NOT EXISTS content_library_assets_bucket_key_uidx
    ON public.content_library_assets (bucket_name, s3_key);
CREATE INDEX IF NOT EXISTS content_library_assets_checksum_idx
    ON public.content_library_assets (checksum_sha256)
    WHERE checksum_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS content_library_assets_duplicate_idx
    ON public.content_library_assets (duplicate_of_asset_id)
    WHERE duplicate_of_asset_id IS NOT NULL;
-- Selection only ever considers active, canonical rows.
CREATE INDEX IF NOT EXISTS content_library_assets_active_selection_idx
    ON public.content_library_assets (cityid, asset_type, category, subcategory)
    WHERE status = 'active' AND duplicate_of_asset_id IS NULL;
CREATE INDEX IF NOT EXISTS content_library_assets_last_seen_idx
    ON public.content_library_assets (last_seen_at);
CREATE INDEX IF NOT EXISTS content_library_assets_status_idx
    ON public.content_library_assets (status);
CREATE INDEX IF NOT EXISTS content_library_assets_cityid_idx
    ON public.content_library_assets (cityid);
CREATE INDEX IF NOT EXISTS content_library_assets_type_idx
    ON public.content_library_assets (type);
CREATE INDEX IF NOT EXISTS content_library_hook_gin
    ON public.content_library_assets USING GIN (hook_compatibility);

DROP TRIGGER IF EXISTS content_library_assets_set_updated_at
    ON public.content_library_assets;
CREATE TRIGGER content_library_assets_set_updated_at
BEFORE UPDATE ON public.content_library_assets
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ---------------------------------------------------------------------------
-- Inventory runs
--
-- listing_complete is what makes "never mark missing after an incomplete
-- listing" survive a process restart. Reconciliation reads it; it is not
-- merely a report field.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.content_library_inventory_runs (
    id                BIGSERIAL   PRIMARY KEY,
    run_id            TEXT        NOT NULL UNIQUE,
    bucket_name       TEXT        NOT NULL,
    prefix            TEXT        NOT NULL,
    dry_run           BOOLEAN     NOT NULL DEFAULT false,
    listing_complete  BOOLEAN     NOT NULL DEFAULT false,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,

    objects_listed    INTEGER     NOT NULL DEFAULT 0,
    markers_skipped   INTEGER     NOT NULL DEFAULT 0,
    exported_skipped  INTEGER     NOT NULL DEFAULT 0,
    discovered        INTEGER     NOT NULL DEFAULT 0,
    unchanged         INTEGER     NOT NULL DEFAULT 0,
    metadata_changed  INTEGER     NOT NULL DEFAULT 0,
    marked_missing    INTEGER     NOT NULL DEFAULT 0,
    probed            INTEGER     NOT NULL DEFAULT 0,
    probe_failures    INTEGER     NOT NULL DEFAULT 0,
    checksummed       INTEGER     NOT NULL DEFAULT 0,
    duplicates_found  INTEGER     NOT NULL DEFAULT 0,
    unresolved_cityid INTEGER     NOT NULL DEFAULT 0,
    error_detail      TEXT
);

CREATE INDEX IF NOT EXISTS content_library_inventory_runs_started_idx
    ON public.content_library_inventory_runs (started_at DESC);

-- ---------------------------------------------------------------------------
-- Controlled vocabularies
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.content_library_tags (
    id           BIGSERIAL   PRIMARY KEY,
    namespace    TEXT        NOT NULL,   -- theme|mood|visual|audience|compatibility|emotion
    slug         TEXT        NOT NULL,
    display_name TEXT        NOT NULL,
    aliases      TEXT[]      NOT NULL DEFAULT '{}',
    active       BOOLEAN     NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, slug)
);

CREATE TABLE IF NOT EXISTS public.content_library_asset_tags (
    asset_id    BIGINT      NOT NULL REFERENCES public.content_library_assets(id) ON DELETE CASCADE,
    tag_id      BIGINT      NOT NULL REFERENCES public.content_library_tags(id) ON DELETE RESTRICT,
    provenance  TEXT        NOT NULL DEFAULT 'human',
    confidence  NUMERIC(5,4),
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, tag_id),
    CHECK (provenance IN ('path','filename','probe','ai','human','import')),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS content_library_asset_tags_tag_idx
    ON public.content_library_asset_tags (tag_id, asset_id);

CREATE TABLE IF NOT EXISTS public.content_library_hooks (
    id           BIGSERIAL   PRIMARY KEY,
    slug         TEXT        NOT NULL UNIQUE,
    display_name TEXT        NOT NULL,
    default_copy TEXT,
    active       BOOLEAN     NOT NULL DEFAULT true,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.content_library_asset_hooks (
    asset_id   BIGINT      NOT NULL REFERENCES public.content_library_assets(id) ON DELETE CASCADE,
    hook_id    BIGINT      NOT NULL REFERENCES public.content_library_hooks(id) ON DELETE RESTRICT,
    provenance TEXT        NOT NULL DEFAULT 'human',
    confidence NUMERIC(5,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, hook_id),
    CHECK (provenance IN ('ai','human','import')),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

-- ---------------------------------------------------------------------------
-- AI enrichment proposals
--
-- AI output lands here and never writes directly to the asset row. Accepted
-- values are promoted deliberately.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.content_library_asset_enrichments (
    id                BIGSERIAL   PRIMARY KEY,
    asset_id          BIGINT      NOT NULL REFERENCES public.content_library_assets(id) ON DELETE CASCADE,
    model_name        TEXT        NOT NULL,
    prompt_version    TEXT        NOT NULL,
    input_fingerprint TEXT        NOT NULL,
    raw_response      JSONB       NOT NULL,
    proposed_patch    JSONB       NOT NULL,
    confidence        NUMERIC(5,4),
    state             TEXT        NOT NULL DEFAULT 'proposed',
    reviewed_by       TEXT,
    reviewed_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (asset_id, model_name, prompt_version, input_fingerprint),
    CHECK (state IN ('proposed','accepted','rejected')),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

-- ---------------------------------------------------------------------------
-- Render lineage
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.content_library_renders (
    id                 BIGSERIAL   PRIMARY KEY,
    render_id          TEXT        NOT NULL UNIQUE,    -- RND-{ULID}
    environment        TEXT        NOT NULL,
    state              TEXT        NOT NULL DEFAULT 'draft',
    cityid             TEXT,
    topic              TEXT,
    platform           TEXT[],
    template_id        TEXT        NOT NULL,
    template_version   INTEGER     NOT NULL,
    target_duration_ms BIGINT,
    actual_duration_ms BIGINT,
    brief              JSONB       NOT NULL,
    recipe             JSONB,
    selection_seed     TEXT,
    copy_model         TEXT,
    prompt_version     TEXT,
    renderer_name      TEXT,
    renderer_version   TEXT,
    error_code         TEXT,
    error_detail       TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at         TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    CHECK (environment IN ('dev','staging','prod')),
    CHECK (state IN ('draft','planned','queued','rendering','validating','succeeded','failed','cancelled')),
    CHECK (target_duration_ms IS NULL OR target_duration_ms > 0),
    CHECK (actual_duration_ms IS NULL OR actual_duration_ms > 0)
);

DROP TRIGGER IF EXISTS content_library_renders_set_updated_at
    ON public.content_library_renders;
CREATE TRIGGER content_library_renders_set_updated_at
BEFORE UPDATE ON public.content_library_renders
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE INDEX IF NOT EXISTS content_library_renders_state_created_idx
    ON public.content_library_renders (state, created_at);

CREATE TABLE IF NOT EXISTS public.content_library_render_assets (
    render_id      BIGINT  NOT NULL REFERENCES public.content_library_renders(id) ON DELETE CASCADE,
    asset_id       BIGINT  NOT NULL REFERENCES public.content_library_assets(id) ON DELETE RESTRICT,
    sequence_no    INTEGER NOT NULL,
    role           TEXT    NOT NULL,
    source_in_ms   BIGINT  NOT NULL DEFAULT 0,
    source_out_ms  BIGINT  NOT NULL,
    timeline_in_ms BIGINT  NOT NULL,
    transform      JSONB   NOT NULL DEFAULT '{}',
    audio_policy   JSONB   NOT NULL DEFAULT '{}',
    PRIMARY KEY (render_id, sequence_no),
    CHECK (sequence_no >= 0),
    CHECK (source_in_ms >= 0),
    CHECK (source_out_ms > source_in_ms),
    CHECK (timeline_in_ms >= 0)
);

CREATE INDEX IF NOT EXISTS content_library_render_assets_asset_idx
    ON public.content_library_render_assets (asset_id, render_id);

-- Generated media may only ever live under ugc-assets/exported/. This CHECK
-- is the database half of that guarantee; the IAM policy on server 3 is the
-- other half.
CREATE TABLE IF NOT EXISTS public.content_library_render_artifacts (
    id              BIGSERIAL   PRIMARY KEY,
    render_id       BIGINT      NOT NULL REFERENCES public.content_library_renders(id) ON DELETE CASCADE,
    role            TEXT        NOT NULL,
    bucket_name     TEXT        NOT NULL DEFAULT 'big-city-travel-guide-clips',
    s3_key          TEXT        NOT NULL,
    s3_version_id   TEXT,
    size_bytes      BIGINT,
    content_type    TEXT,
    checksum_sha256 TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bucket_name, s3_key),
    UNIQUE (render_id, role),
    CHECK (s3_key LIKE 'ugc-assets/exported/%')
);

-- ---------------------------------------------------------------------------
-- Ownership
-- ---------------------------------------------------------------------------
ALTER TABLE public.content_library_assets             OWNER TO bigcity;
ALTER TABLE public.content_library_inventory_runs     OWNER TO bigcity;
ALTER TABLE public.content_library_tags               OWNER TO bigcity;
ALTER TABLE public.content_library_asset_tags         OWNER TO bigcity;
ALTER TABLE public.content_library_hooks              OWNER TO bigcity;
ALTER TABLE public.content_library_asset_hooks        OWNER TO bigcity;
ALTER TABLE public.content_library_asset_enrichments  OWNER TO bigcity;
ALTER TABLE public.content_library_renders            OWNER TO bigcity;
ALTER TABLE public.content_library_render_assets      OWNER TO bigcity;
ALTER TABLE public.content_library_render_artifacts   OWNER TO bigcity;

COMMIT;
