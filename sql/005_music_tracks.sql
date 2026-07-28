-- MediaMixer music tracks, applied to the mediamixer database on server 3.
-- Run as the bigcity owner. Idempotent — safe to run regardless of what was
-- applied before. Depends on 001 (public.set_updated_at, content_library_renders).
--
-- Why a table of its own rather than asset_type='music' on
-- content_library_assets: a music bed carries licensing that a clip never
-- does — a licence class, an attribution string, and whether the terms even
-- permit commercial use. These Reels promote a paid product, so the two
-- traps are (1) a non-commercial licence used commercially, and (2) an
-- attribution the licence requires but the post omits. Both are properties
-- of a track, recorded here, so the render step can enforce them instead of
-- relying on someone remembering per video.

BEGIN;

-- ---------------------------------------------------------------------------
-- Music tracks
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.content_library_music_tracks (
    id                     BIGSERIAL   PRIMARY KEY,

    -- Identity
    track_id               TEXT        UNIQUE,          -- assigned, e.g. MUS-00001
    bucket_name            TEXT        NOT NULL DEFAULT 'big-city-travel-guide-clips',
    s3_key                 TEXT        NOT NULL UNIQUE, -- e.g. ugc-assets/music/upbeat-01.mp3
    s3_version_id          TEXT,
    filename               TEXT,
    folder                 TEXT,

    -- Object facts, from S3
    size_bytes             BIGINT,
    content_type           TEXT,
    etag                   TEXT,
    s3_last_modified_at    TIMESTAMPTZ,
    checksum_sha256        TEXT,

    -- Media facts, measured by ffprobe — never entered by hand
    duration_ms            BIGINT,
    sample_rate            INTEGER,
    channels               SMALLINT,
    audio_codec            TEXT,
    probe_data             JSONB,
    probe_error            TEXT,

    -- Descriptive
    title                  TEXT,
    artist                 TEXT,
    album                  TEXT,
    genre                  TEXT,
    mood                   TEXT,
    bpm                    NUMERIC(6,2),
    energy                 SMALLINT,                    -- 1..5, for matching a cut's pace
    instrumental           BOOLEAN,
    tags                   TEXT[]      NOT NULL DEFAULT '{}',
    notes                  TEXT,

    -- Licensing & attribution. license is the class; the booleans are the two
    -- questions that actually gate use, kept explicit rather than inferred so
    -- an odd bespoke licence can still be recorded truthfully.
    license                TEXT        NOT NULL DEFAULT 'unknown',
    license_url            TEXT,
    source                 TEXT,                        -- e.g. 'YouTube Audio Library'
    source_url             TEXT,
    commercial_use_allowed BOOLEAN,                     -- NULL = not yet confirmed
    derivatives_allowed    BOOLEAN,                     -- cutting under video is a derivative
    attribution_required   BOOLEAN     NOT NULL DEFAULT false,
    attribution_text       TEXT,                        -- ready-to-use credit line (TASL)
    license_expires_at     TIMESTAMPTZ,                 -- for time-limited licences
    license_proof_s3_key   TEXT,                        -- screenshot/receipt of the terms

    -- Governance
    status                 TEXT        NOT NULL DEFAULT 'discovered',
    reviewed_by            TEXT,
    reviewed_at            TIMESTAMPTZ,

    -- Operations
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT music_tracks_duration_ms_check
        CHECK (duration_ms IS NULL OR duration_ms > 0),
    CONSTRAINT music_tracks_energy_check
        CHECK (energy IS NULL OR energy BETWEEN 1 AND 5),
    -- cc0/public_domain need no attribution; the -nc classes forbid our
    -- commercial use; -nd forbids cutting the track under video.
    CONSTRAINT music_tracks_license_check
        CHECK (license IN (
            'unknown','cc0','public_domain',
            'cc_by','cc_by_sa','cc_by_nc','cc_by_nc_sa','cc_by_nd','cc_by_nc_nd',
            'royalty_free','rights_managed','licensed','proprietary')),
    CONSTRAINT music_tracks_status_check
        CHECK (status IN
            ('discovered','probing','needs_review','active','rejected','missing','error','archived')),
    -- A track a reviewer has approved for use must carry the attribution its
    -- licence demands. Enforced only at 'active' so an in-progress import can
    -- sit in needs_review without the credit line yet written.
    CONSTRAINT music_tracks_attribution_present_when_active
        CHECK (status <> 'active'
               OR attribution_required = false
               OR (attribution_text IS NOT NULL AND btrim(attribution_text) <> ''))
);

CREATE UNIQUE INDEX IF NOT EXISTS content_library_music_tracks_bucket_key_uidx
    ON public.content_library_music_tracks (bucket_name, s3_key);
CREATE INDEX IF NOT EXISTS content_library_music_tracks_status_idx
    ON public.content_library_music_tracks (status);
CREATE INDEX IF NOT EXISTS content_library_music_tracks_license_idx
    ON public.content_library_music_tracks (license);
CREATE INDEX IF NOT EXISTS content_library_music_tracks_tags_gin
    ON public.content_library_music_tracks USING GIN (tags);
CREATE INDEX IF NOT EXISTS content_library_music_tracks_checksum_idx
    ON public.content_library_music_tracks (checksum_sha256)
    WHERE checksum_sha256 IS NOT NULL;
-- Selection only ever considers cleared, commercially-usable tracks.
CREATE INDEX IF NOT EXISTS content_library_music_tracks_usable_idx
    ON public.content_library_music_tracks (mood, energy)
    WHERE status = 'active' AND commercial_use_allowed = true;

DROP TRIGGER IF EXISTS content_library_music_tracks_set_updated_at
    ON public.content_library_music_tracks;
CREATE TRIGGER content_library_music_tracks_set_updated_at
BEFORE UPDATE ON public.content_library_music_tracks
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ---------------------------------------------------------------------------
-- Render → music lineage
--
-- Records which track a render actually used, so the attribution a finished
-- video owes can be reconstructed from the render alone. ON DELETE RESTRICT
-- on the track keeps a credited track from being deleted out from under a
-- render that still needs to attribute it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.content_library_render_music (
    render_id      BIGINT  NOT NULL REFERENCES public.content_library_renders(id) ON DELETE CASCADE,
    track_id       BIGINT  NOT NULL REFERENCES public.content_library_music_tracks(id) ON DELETE RESTRICT,
    sequence_no    INTEGER NOT NULL DEFAULT 0,
    role           TEXT    NOT NULL DEFAULT 'bed',      -- bed|sting|transition
    source_in_ms   BIGINT  NOT NULL DEFAULT 0,
    source_out_ms  BIGINT,
    timeline_in_ms BIGINT  NOT NULL DEFAULT 0,
    gain_db        NUMERIC(5,2),
    PRIMARY KEY (render_id, sequence_no),
    CHECK (sequence_no >= 0),
    CHECK (source_in_ms >= 0),
    CHECK (source_out_ms IS NULL OR source_out_ms > source_in_ms),
    CHECK (timeline_in_ms >= 0)
);

CREATE INDEX IF NOT EXISTS content_library_render_music_track_idx
    ON public.content_library_render_music (track_id, render_id);

-- ---------------------------------------------------------------------------
-- Ownership
-- ---------------------------------------------------------------------------
ALTER TABLE public.content_library_music_tracks  OWNER TO bigcity;
ALTER TABLE public.content_library_render_music   OWNER TO bigcity;

COMMIT;
