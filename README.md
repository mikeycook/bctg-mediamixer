# MediaMixer

Controlled AI media-generation pipeline for Big City Travel Guide.

Answers a brief such as *"make a 20-second New York pizza Reel"* by selecting
eligible clips from the UGC library, producing a deterministic edit plan,
rendering a vertical video, and recording exactly what was used. It is a media
pipeline with AI-assisted decisions — not a prompt that edits the bucket.

## Current phase

Phases 1–2: schema, S3 inventory, media probing, checksums, and reaction-alias
merging. Rendering is deliberately not built until the catalog can distinguish
active, review-required, missing, and rejected assets.

## Layout

```
sql/          Schema, applied with psql. No migration framework.
migration/    One-time export from server 2 and seed of this database.
api/          FastAPI content library service (reached via server 2's proxy).
tests/        pytest; runs with no database, no AWS, no ffprobe.
deploy/       systemd units.
docs/         Design package — start with ai-media-generation/09.
```

Pipeline modules are flat at the repository root, matching the convention in
the sibling `genAITest` repository: `S3Interpreter.py`,
`ContentLibraryPaths.py`, `ContentLibraryProbe.py`, `ContentLibrarySync.py`.

## Setup

```bash
python3 -m venv venv
. venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The test suite is hermetic — no database, no AWS credentials, no `ffprobe`
binary required.

## Configuration

All runtime configuration comes from the environment. On server 3 that is
`/etc/mediamixer/mediamixer.env`, `chmod 600`, loaded by systemd:

```
DATABASE_URL=postgresql://bigcity:...@mediamixerdb....us-east-1.rds.amazonaws.com:5432/mediamixer
CLIPS_BUCKET=big-city-travel-guide-clips
CLIPS_PREFIX=ugc-assets/
CLIPS_REGION=us-east-1
```

There are **no AWS credentials** in that file or anywhere in this repository.
S3 access comes from the EC2 instance role.

## Applying the schema

```bash
psql "$DATABASE_URL" -f sql/001_content_library_schema.sql
```

Idempotent, so it is safe to re-run.

## Safety properties

Source objects are never moved, renamed, deleted, or overwritten. Generated
media is written only beneath `ugc-assets/exported/`, enforced by both the IAM
policy on server 3 and a `CHECK` constraint on the artifacts table. The
inventory sync is read-only against S3 — `S3Interpreter` has no write methods
at all — and offers `--dry-run` for verification before any database write.

The golden source database on server 2 is read exactly once, to copy one
table, and is never modified.

See `CLAUDE.md` for the full set of decisions and constraints.
