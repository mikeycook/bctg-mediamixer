# VS Code Implementation Plan

## Recommended stack

Use the language already dominant in the application repository. If no media service exists, Python is a pragmatic default for orchestration and PostgreSQL access, with `ffprobe`/`ffmpeg` as external media tools.

Keep AWS, database, and model integrations behind interfaces so tests can use local fixtures.

## Initial EC2 deployment

Build the first release to run on the EC2 server with private/local access to the
PostgreSQL database. Package the service as a container or supervised system
service, install `ffmpeg`/`ffprobe`, and use an EC2 IAM role for S3 access.

The process needs:

- database connectivity without exposing PostgreSQL publicly;
- S3 list/read on `ugc-assets/`;
- S3 write only on `ugc-assets/exported/`;
- local scratch capacity with enforced quotas and cleanup;
- outbound access to the selected AI service, if enrichment is enabled;
- a scheduler/queue worker; and
- centralized logs and health checks.

Keep inventory, selection, and rendering as separable modules. If rendering later
exceeds the server's CPU/disk capacity, move only render workers to additional EC2
instances, ECS, or AWS Batch.

## Suggested repository structure

```text
media-pipeline/
├── README.md
├── pyproject.toml
├── migrations/
├── src/
│   └── media_pipeline/
│       ├── config.py
│       ├── db/
│       ├── inventory/
│       ├── probe/
│       ├── enrichment/
│       ├── review/
│       ├── selection/
│       ├── recipes/
│       ├── rendering/
│       ├── validation/
│       └── exports/
├── templates/
├── prompts/
├── tests/
│   ├── fixtures/
│   ├── unit/
│   └── integration/
└── docs/
```

## Phase 0: Confirm contracts

Deliver:

- copy these design documents into the implementation repository;
- identify the authoritative cities table/key type;
- confirm bucket versioning and IAM;
- confirm rights owner/reviewer workflow;
- select runtime/deployment target.

Acceptance:

- unresolved decisions are written as explicit ADRs;
- no code assumes `cityid` data type or FK target before confirmation.

## Phase 1: Database migration

Deliver:

- apply `001_content_library_v2.sql` in staging;
- backfill normalized `asset_type`;
- create vocabulary seeds;
- add repository/data-access layer;
- produce constraint-violation reports.

Acceptance:

- migration is repeatably deployed through the repository’s migration tool;
- legacy reads remain operational;
- `updated_at` changes on updates;
- render lineage tables support a full recipe.

## Phase 2: Inventory and probing

Deliver:

- S3 inventory command;
- zero-byte marker and `exported/` exclusions;
- idempotent upsert;
- missing-object reconciliation;
- `ffprobe` extraction;
- checksum job;
- inventory-run report.

Acceptance against the refreshed 2026-07-25 S3 snapshot:

- exactly 73 source media objects registered;
- exactly 6 classified as app, 36 as B-roll, and 31 as reaction;
- 40 resolve to New York and 2 to Tokyo, subject to city-table mapping;
- 31 reaction objects collapse to 17 unique payloads for selection;
- no empty folder markers registered;
- rerunning produces no duplicate rows.

## Phase 3: Metadata enrichment and review

Deliver:

- structured enrichment schema;
- versioned prompt;
- representative-frame/proxy generation;
- proposal storage;
- simple review API/UI or command workflow;
- rights fields and activation gate.

Acceptance:

- AI output cannot directly activate an asset;
- reviewers can accept/reject individual proposed fields/tags;
- all 73 object rows can reach `active`, `rejected`, `error`, or a reviewed
  duplicate-alias state with audit data.

## Phase 4: Search and selection

Deliver:

- typed video brief;
- eligibility query;
- ranked candidate selection;
- diversity and recent-use constraints;
- deterministic seed option;
- recipe JSON Schema;
- insufficient-assets diagnostics.

Acceptance:

- New York pizza brief can produce a recipe when required assets are active;
- Tokyo B-roll brief fails clearly with current inventory;
- reaction-required brief uses only reviewed, rights-cleared, active reactions;
- byte-identical reaction aliases merge their folder-derived emotion tags but never
  appear together or receive duplicate weight;
- wrong-city and rights-unknown assets are never silently substituted.

## Phase 5: Renderer and validation

Deliver:

- FFmpeg filter-graph builder;
- media normalization;
- text/caption safe areas;
- audio keep/lower/mute modes;
- MP4 output;
- technical QA;
- retry and error reporting.

Acceptance:

- renders 1080×1920 H.264/AAC at 30 fps;
- preserves source objects;
- recipe source trims match output;
- deterministic fixture render passes technical validation.

## Phase 6: S3 export and lineage

Deliver:

- immutable render ID;
- required export layout;
- final, preview, thumbnail, captions, manifest, validation artifacts;
- checksums;
- transactional database finalization.

Acceptance:

- every generated media object is under `ugc-assets/exported/`;
- no source prefix receives generated media;
- a succeeded render resolves every input and output;
- rerender creates a new render ID rather than overwriting.

## Phase 7: Operational hardening

Deliver:

- least-privilege IAM;
- secrets management;
- queue/worker leases;
- metrics and structured logs;
- dead-letter handling;
- cost controls;
- backups and restore test;
- lifecycle rules for previews/cache.

Acceptance:

- failed jobs are diagnosable and retry-safe;
- incomplete S3 listings cannot mark the library missing;
- no secrets or signed URLs appear in logs.

## Test strategy

- Unit: path parsing, slug normalization, eligibility, ranking, recipe validation.
- Contract: S3 adapter, database repositories, model response schema.
- Integration: PostgreSQL + S3 emulator/test prefix + real `ffprobe`.
- Golden media: short licensed fixtures with expected probe/render properties.
- End-to-end: brief → recipe → render → validation → exported artifacts.

Do not commit the user’s actual media to the code repository.

## First VS Code instruction

> Read the complete AI Media Generation design package. Implement Phases 1 and 2
> on the EC2 server that has private access to PostgreSQL. Preserve all existing S3
> keys, exclude zero-byte markers and `ugc-assets/exported/` from source ingestion,
> make sync idempotent, identify duplicate payloads by SHA-256, and demonstrate
> acceptance against the 73-object 2026-07-25 S3 snapshot. Do not implement
> rendering or social publishing yet.
