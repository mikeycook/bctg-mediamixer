# Metadata and Data-Model Review

## Assessment of the current table

`public.content_library_assets` is a sound starting point: it has a stable surrogate key, human-facing asset ID, unique S3 key, useful semantic columns, timestamps, and a GIN index for hook phrases.

The main issues are:

1. `duration TEXT` prevents reliable numeric range queries.
2. Technical properties such as dimensions, orientation, codecs, frame rate, audio, and checksum are absent.
3. Bucket and object version are implicit.
4. `type`, `subtype`, `category`, and `subcategory` are unrestricted text and will drift.
5. `hook_compatibility` stores phrases as an array, which is hard to govern and analyze.
6. There is no review/status lifecycle.
7. There is no provenance/confidence for AI-generated metadata.
8. There is no rights/license state.
9. There is no soft-missing state for objects removed from S3.
10. There is no render, recipe, artifact, or asset-usage lineage.
11. `updated_at DEFAULT now()` does not automatically update on row changes.
12. `cityid` has no declared foreign key in the supplied DDL.

## Recommended strategy

Evolve the existing table in place to minimize disruption, then add normalized tag and render-lineage tables. Preserve legacy fields initially and migrate consumers gradually.

### Rename versus compatibility

Prefer adding `asset_type` while retaining legacy `type` during transition. Backfill normalized values, switch application code, and remove or convert legacy fields only in a later migration.

### Duration

Add `duration_ms BIGINT CHECK (duration_ms > 0)`. Retain `duration TEXT` temporarily for source compatibility; ingestion writes the measured numeric field. Do not parse uncertain legacy strings destructively.

### Tags and hooks

Use normalized vocabulary tables:

- `content_library_tags`
- `content_library_asset_tags`
- `content_library_hooks`
- `content_library_asset_hooks`

This supports aliases, confidence, provenance, review, analytics, and exact filtering. Keep `hook_compatibility` during transition.

### Workflow state

Recommended `status` values:

- `discovered`
- `probing`
- `needs_review`
- `active`
- `rejected`
- `missing`
- `error`
- `archived`

Only `active` assets are eligible for production selection.

### Rights

Recommended `rights_status` values:

- `unknown`
- `owned`
- `licensed`
- `restricted`
- `expired`

Production selection should allow only `owned` and `licensed`, with a valid date range when applicable.

## Core asset fields

| Area | Recommended fields |
|---|---|
| Identity | `id`, `asset_id`, `bucket_name`, `s3_key`, `s3_version_id` |
| Object facts | `size_bytes`, `content_type`, `etag`, `s3_last_modified_at`, `checksum_sha256` |
| Video facts | `duration_ms`, `width`, `height`, `frame_rate`, `video_codec`, `audio_codec`, `has_audio`, `orientation` |
| Semantics | `asset_type`, `subtype`, `category`, `subcategory`, `place_name`, `cityid`, `country_code`, `notes` |
| Editorial | `shot_type`, `camera_motion`, `time_of_day`, `people_present`, `speech_present`, `quality_score`, `city_agnostic` |
| Governance | `status`, `rights_status`, `rights_source`, `rights_expires_at`, `reviewed_by`, `reviewed_at` |
| Operations | `first_seen_at`, `last_seen_at`, `missing_since`, `created_at`, `updated_at` |

When two keys have identical payloads, retain both rows for S3 lineage and set
`duplicate_of_asset_id` on the non-canonical rows. Folder placement is intentional
classification: merge the reaction labels derived from every alias onto the
canonical media identity. Only the canonical row is eligible for selection, but a
query for any of its merged reaction labels may return it. Do not automatically
assign aliases from ETag alone; confirm with SHA-256.

## Render model

`content_library_renders` stores the brief, template, state, recipe, renderer/model versions, and output duration. `content_library_render_assets` stores the ordered source assets and trim windows. `content_library_render_artifacts` stores each exported object.

This provides:

- traceability;
- duplicate-use limits;
- reproducibility;
- “never published” queries;
- quality diagnostics; and
- future performance attribution.

## AI metadata provenance

Do not overwrite human-reviewed fields directly with AI output. Store each AI enrichment run in `content_library_asset_enrichments` with:

- model and prompt version;
- raw structured response;
- proposed patch;
- confidence;
- state (`proposed`, `accepted`, `rejected`);
- timestamps and reviewer.

Accepted values are then promoted into the canonical asset row/tag associations.

## Index recommendations

- Partial selection index on active assets by `(cityid, asset_type, category)`.
- GIN only where array/JSON containment is deliberately used.
- Unique `(bucket_name, s3_key)`.
- Unique checksum when deduplication policy is mature; initially use a non-unique checksum index because identical files may intentionally exist at different keys.
- Render state and creation-time indexes for workers.
- Tag/hook reverse indexes by asset ID and vocabulary ID.

## Migration

See `sql/001_content_library_v2.sql`. It is intentionally forward-only and conservative:

- keeps legacy columns;
- avoids object moves;
- adds constraints as `NOT VALID` where existing data may be inconsistent;
- creates the automatic `updated_at` trigger; and
- introduces render lineage.

Run it in a staging database, inspect constraint violations, and only then validate constraints in production.
