BEGIN;

-- Conservative forward migration. Legacy columns remain available while clients migrate.
ALTER TABLE public.content_library_assets
    ADD COLUMN IF NOT EXISTS bucket_name TEXT NOT NULL DEFAULT 'big-city-travel-guide-clips',
    ADD COLUMN IF NOT EXISTS s3_version_id TEXT,
    ADD COLUMN IF NOT EXISTS etag TEXT,
    ADD COLUMN IF NOT EXISTS s3_last_modified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS checksum_sha256 TEXT,
    ADD COLUMN IF NOT EXISTS duplicate_of_asset_id BIGINT
        REFERENCES public.content_library_assets(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS duration_ms BIGINT,
    ADD COLUMN IF NOT EXISTS width INTEGER,
    ADD COLUMN IF NOT EXISTS height INTEGER,
    ADD COLUMN IF NOT EXISTS frame_rate NUMERIC(10,4),
    ADD COLUMN IF NOT EXISTS video_codec TEXT,
    ADD COLUMN IF NOT EXISTS audio_codec TEXT,
    ADD COLUMN IF NOT EXISTS has_audio BOOLEAN,
    ADD COLUMN IF NOT EXISTS orientation TEXT,
    ADD COLUMN IF NOT EXISTS asset_type TEXT,
    ADD COLUMN IF NOT EXISTS country_code CHAR(2),
    ADD COLUMN IF NOT EXISTS shot_type TEXT,
    ADD COLUMN IF NOT EXISTS camera_motion TEXT,
    ADD COLUMN IF NOT EXISTS time_of_day TEXT,
    ADD COLUMN IF NOT EXISTS people_present BOOLEAN,
    ADD COLUMN IF NOT EXISTS speech_present BOOLEAN,
    ADD COLUMN IF NOT EXISTS city_agnostic BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS quality_score SMALLINT,
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'discovered',
    ADD COLUMN IF NOT EXISTS rights_status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS rights_source TEXT,
    ADD COLUMN IF NOT EXISTS rights_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reviewed_by TEXT,
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS missing_since TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS probe_data JSONB;

-- Normalize currently documented legacy type values without discarding the source column.
UPDATE public.content_library_assets
SET asset_type = CASE lower(trim(type))
    WHEN 'b-roll' THEN 'broll'
    WHEN 'broll' THEN 'broll'
    WHEN 'app' THEN 'app'
    WHEN 'reaction' THEN 'reaction'
    WHEN 'music' THEN 'music'
    WHEN 'voiceover' THEN 'voiceover'
    WHEN 'caption' THEN 'caption'
    WHEN 'cta' THEN 'cta'
    ELSE lower(trim(type))
END
WHERE asset_type IS NULL AND type IS NOT NULL;

ALTER TABLE public.content_library_assets
    DROP CONSTRAINT IF EXISTS content_library_assets_duration_ms_check,
    ADD CONSTRAINT content_library_assets_duration_ms_check
        CHECK (duration_ms IS NULL OR duration_ms > 0) NOT VALID,
    DROP CONSTRAINT IF EXISTS content_library_assets_dimensions_check,
    ADD CONSTRAINT content_library_assets_dimensions_check
        CHECK ((width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)) NOT VALID,
    DROP CONSTRAINT IF EXISTS content_library_assets_orientation_check,
    ADD CONSTRAINT content_library_assets_orientation_check
        CHECK (orientation IS NULL OR orientation IN ('portrait', 'landscape', 'square')) NOT VALID,
    DROP CONSTRAINT IF EXISTS content_library_assets_quality_score_check,
    ADD CONSTRAINT content_library_assets_quality_score_check
        CHECK (quality_score IS NULL OR quality_score BETWEEN 1 AND 5) NOT VALID,
    DROP CONSTRAINT IF EXISTS content_library_assets_status_check,
    ADD CONSTRAINT content_library_assets_status_check
        CHECK (status IN ('discovered','probing','needs_review','active','rejected','missing','error','archived')) NOT VALID,
    DROP CONSTRAINT IF EXISTS content_library_assets_rights_status_check,
    ADD CONSTRAINT content_library_assets_rights_status_check
        CHECK (rights_status IN ('unknown','owned','licensed','restricted','expired')) NOT VALID;

CREATE UNIQUE INDEX IF NOT EXISTS content_library_assets_bucket_key_uidx
    ON public.content_library_assets (bucket_name, s3_key);
CREATE INDEX IF NOT EXISTS content_library_assets_checksum_idx
    ON public.content_library_assets (checksum_sha256)
    WHERE checksum_sha256 IS NOT NULL;
CREATE INDEX IF NOT EXISTS content_library_assets_duplicate_idx
    ON public.content_library_assets (duplicate_of_asset_id)
    WHERE duplicate_of_asset_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS content_library_assets_active_selection_idx
    ON public.content_library_assets (cityid, asset_type, category, subcategory)
    WHERE status = 'active' AND duplicate_of_asset_id IS NULL;
CREATE INDEX IF NOT EXISTS content_library_assets_last_seen_idx
    ON public.content_library_assets (last_seen_at);

CREATE OR REPLACE FUNCTION public.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS content_library_assets_set_updated_at
    ON public.content_library_assets;
CREATE TRIGGER content_library_assets_set_updated_at
BEFORE UPDATE ON public.content_library_assets
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

CREATE TABLE IF NOT EXISTS public.content_library_tags (
    id BIGSERIAL PRIMARY KEY,
    namespace TEXT NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, slug)
);

CREATE TABLE IF NOT EXISTS public.content_library_asset_tags (
    asset_id BIGINT NOT NULL REFERENCES public.content_library_assets(id) ON DELETE CASCADE,
    tag_id BIGINT NOT NULL REFERENCES public.content_library_tags(id) ON DELETE RESTRICT,
    provenance TEXT NOT NULL DEFAULT 'human',
    confidence NUMERIC(5,4),
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, tag_id),
    CHECK (provenance IN ('path','filename','probe','ai','human','import')),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE INDEX IF NOT EXISTS content_library_asset_tags_tag_idx
    ON public.content_library_asset_tags (tag_id, asset_id);

CREATE TABLE IF NOT EXISTS public.content_library_hooks (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    default_copy TEXT,
    active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.content_library_asset_hooks (
    asset_id BIGINT NOT NULL REFERENCES public.content_library_assets(id) ON DELETE CASCADE,
    hook_id BIGINT NOT NULL REFERENCES public.content_library_hooks(id) ON DELETE RESTRICT,
    provenance TEXT NOT NULL DEFAULT 'human',
    confidence NUMERIC(5,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, hook_id),
    CHECK (provenance IN ('ai','human','import')),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS public.content_library_asset_enrichments (
    id BIGSERIAL PRIMARY KEY,
    asset_id BIGINT NOT NULL REFERENCES public.content_library_assets(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    raw_response JSONB NOT NULL,
    proposed_patch JSONB NOT NULL,
    confidence NUMERIC(5,4),
    state TEXT NOT NULL DEFAULT 'proposed',
    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (asset_id, model_name, prompt_version, input_fingerprint),
    CHECK (state IN ('proposed','accepted','rejected')),
    CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
);

CREATE TABLE IF NOT EXISTS public.content_library_renders (
    id BIGSERIAL PRIMARY KEY,
    render_id TEXT NOT NULL UNIQUE,
    environment TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft',
    cityid TEXT,
    topic TEXT,
    platform TEXT[],
    template_id TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    target_duration_ms BIGINT,
    actual_duration_ms BIGINT,
    brief JSONB NOT NULL,
    recipe JSONB,
    selection_seed TEXT,
    copy_model TEXT,
    prompt_version TEXT,
    renderer_name TEXT,
    renderer_version TEXT,
    error_code TEXT,
    error_detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
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
    render_id BIGINT NOT NULL REFERENCES public.content_library_renders(id) ON DELETE CASCADE,
    asset_id BIGINT NOT NULL REFERENCES public.content_library_assets(id) ON DELETE RESTRICT,
    sequence_no INTEGER NOT NULL,
    role TEXT NOT NULL,
    source_in_ms BIGINT NOT NULL DEFAULT 0,
    source_out_ms BIGINT NOT NULL,
    timeline_in_ms BIGINT NOT NULL,
    transform JSONB NOT NULL DEFAULT '{}',
    audio_policy JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (render_id, sequence_no),
    CHECK (sequence_no >= 0),
    CHECK (source_in_ms >= 0),
    CHECK (source_out_ms > source_in_ms),
    CHECK (timeline_in_ms >= 0)
);

CREATE INDEX IF NOT EXISTS content_library_render_assets_asset_idx
    ON public.content_library_render_assets (asset_id, render_id);

CREATE TABLE IF NOT EXISTS public.content_library_render_artifacts (
    id BIGSERIAL PRIMARY KEY,
    render_id BIGINT NOT NULL REFERENCES public.content_library_renders(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    bucket_name TEXT NOT NULL DEFAULT 'big-city-travel-guide-clips',
    s3_key TEXT NOT NULL,
    s3_version_id TEXT,
    size_bytes BIGINT,
    content_type TEXT,
    checksum_sha256 TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bucket_name, s3_key),
    UNIQUE (render_id, role),
    CHECK (s3_key LIKE 'ugc-assets/exported/%')
);

ALTER TABLE public.content_library_tags OWNER TO bigcity;
ALTER TABLE public.content_library_asset_tags OWNER TO bigcity;
ALTER TABLE public.content_library_hooks OWNER TO bigcity;
ALTER TABLE public.content_library_asset_hooks OWNER TO bigcity;
ALTER TABLE public.content_library_asset_enrichments OWNER TO bigcity;
ALTER TABLE public.content_library_renders OWNER TO bigcity;
ALTER TABLE public.content_library_render_assets OWNER TO bigcity;
ALTER TABLE public.content_library_render_artifacts OWNER TO bigcity;

COMMIT;

-- Follow-up after reviewing existing rows:
-- ALTER TABLE public.content_library_assets VALIDATE CONSTRAINT content_library_assets_duration_ms_check;
-- ALTER TABLE public.content_library_assets VALIDATE CONSTRAINT content_library_assets_dimensions_check;
-- ALTER TABLE public.content_library_assets VALIDATE CONSTRAINT content_library_assets_orientation_check;
-- ALTER TABLE public.content_library_assets VALIDATE CONSTRAINT content_library_assets_quality_score_check;
-- ALTER TABLE public.content_library_assets VALIDATE CONSTRAINT content_library_assets_status_check;
-- ALTER TABLE public.content_library_assets VALIDATE CONSTRAINT content_library_assets_rights_status_check;
