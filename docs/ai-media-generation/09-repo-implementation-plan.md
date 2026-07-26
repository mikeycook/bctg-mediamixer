# Repository-Specific Implementation Plan

Scope: Phases 1 and 2 of `08-vscode-implementation-plan.md` — schema,
inventory, probe, checksum, reaction-alias merge — implemented on a
dedicated server, in a dedicated repository, without modifying the golden
source.

Nothing in this document has been executed. It is the plan that precedes
any change to PostgreSQL, S3, or EC2.

---

## 1. Architecture decision

The media pipeline gets its **own server (server 3) and its own database**.
Server 2 — the golden source that the main database is built from — is read
exactly once and never written to.

This replaces the earlier approach of migrating `content_library_assets`
in place on server 2. The reasons it is better:

- **The risky migration disappears.** An `ALTER` against a live production
  database with no point-in-time recovery becomes a `CREATE` against an
  empty one. No `NOT VALID` constraints, no violation report, no staging
  rehearsal, no rollback plan — there is nothing to roll back to.
- **No network path to the golden source.** Server 3 talks to S3 through
  its instance role and to its own database. Server 2's PostgreSQL port
  stays closed, and no credentials for it live on the new box.
- **Blast radius is bounded.** `ffmpeg` filling a disk can take down the
  media pipeline and nothing else.

Existing topology, for reference:

| | Role | Touched by this project |
|---|---|---|
| Server 1 | User settings, favorites | No — never in scope |
| Server 2 | Golden source; main build database | **Read once, at seed time. Never written.** |
| Server 3 | New: media pipeline + render worker | All new work |

### 1.1 Where the media database lives

Recommended: **RDS**, not server 3's local disk. The database is small — 73
rows now, thousands eventually — so a `db.t4g.micro` is inexpensive, and it
buys automated backups and point-in-time recovery from day one rather than a
`pg_dump` cron somebody has to write and monitor. It also makes disk
exhaustion on server 3 unable to touch the database at all.

On-instance PostgreSQL on server 3 is defensible if you would rather stay
consistent with how the rest of the stack runs. If you choose it, put render
scratch on a **separate EBS volume** from the PostgreSQL data directory, and
schedule a dump from the start. Sharing one volume between `ffmpeg` and
PostgreSQL recreates the co-tenancy failure this move was meant to avoid —
with a less critical database behind it, but the failure mode is the same.

### 1.2 Confirmed topology

- **Server 1** — API services plus the user-settings database (favorites).
  Out of scope entirely.
- **Server 2** — the admin tool's backend *and* front end, plus the primary
  build database. The golden source.
- **Server 3** — new. Media pipeline, its own database, its own backend.

The rule worth stating precisely, because it has been applied loosely
earlier in this document: **the golden database is never modified.** Server
2's *application* code is a different matter — deploying to the admin
backend is a routine, reversible operation, and §8 requires about thirty
lines of it.

---

## 2. Conventions inherited from the existing code

Established by reading the repositories, not by preference. The new code
matches these even though it lives elsewhere.

**Python.** `genAITest` is ~255 flat top-level scripts, no package, no
`pyproject.toml`. Shared code is a flat PascalCase module, and `*Interpreter.py`
is the established name for a wrapper around one external system
(`PostgresInterpreter.py`, `GooglePlacesInterpreter.py`). Operational scripts
share one shape: `#!/usr/bin/env python3`, a long docstring with `Usage:` and
`Env (from the systemd EnvironmentFile in the .service, or ./.env locally):`,
`argparse` with `--dry-run`, a local `parse_database_url()` feeding
`PostgresInterpreter`, bracketed `print()` logging, `raise SystemExit(...)`
for fatal misconfiguration, and a summary block that prefixes `"Would be "`
in dry-run. Python 3.12.

**PostgreSQL.** No migration framework anywhere — no Alembic, no runner.
Schema changes are hand-written idempotent `.sql` files applied with `psql`:
`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, `DO $$ ... $$` for
conditional work, a header comment reading "Run as the bigcity owner.
Idempotent — safe to run regardless of what was applied before," and a
closing `ALTER TABLE ... OWNER TO bigcity`.

**AWS.** `boto3` appears in exactly one place — `newguide/backend/backend/app/main.py`
— and is declared in **no** `requirements.txt`. The established pattern
(`main.py:3193`) lazily imports boto3 and constructs
`boto3.client("s3", region_name=_CLIP_REGION)` with no explicit credentials,
already relying on the instance role. Config is `CLIPS_BUCKET`,
`CLIPS_PREFIX`, `CLIPS_REGION`. Media is never downloaded to probe: the
backend presigns a GET URL and hands it to `ffprobe` (`main.py:3206`). That
convention is kept.

**Deployment.** No Dockerfile, compose file, or CI in either tree. Services
run under systemd with an `EnvironmentFile`; artifacts reach EC2 by `scp`
(`deliver_dbs.sh` → `SendDBsToAWS.py`, with remote `sha256sum` verification).

**Testing.** Zero tests exist in either repository — no `pytest.ini`, no
`conftest.py`, no test dependency. `ffprobe` is not installed on the
development Mac. The suite is built from nothing and must run on a laptop
with no database, no AWS credentials, and no `ffprobe`.

### 2.1 The Content Library already exists

`main.py:3163-3331` implements the tab — `GET /admin/content-library`,
`POST /admin/content-library/sync`, `PUT`/`DELETE` — with the React UI at
`newguide/admin_ui/src/ContentLibrary.tsx`. Four behaviors of the existing
sync are corrected in the new implementation:

| Existing behavior | Correction |
|---|---|
| `ON CONFLICT (s3_key) DO NOTHING`, known keys skipped entirely | Real upsert refreshing size, ETag, `last_seen_at` |
| No `ugc-assets/exported/` exclusion | Excluded, so generated media is never ingested as source |
| No missing-object reconciliation | `status='missing'` after a *complete* listing; rows never deleted |
| `duration` stored as `"m:ss"` text | `duration_ms BIGINT`; legacy text retained for UI compatibility |

---

## 3. New repository

`genAITest` cannot be deployed to server 3. It is 77.65 MiB packed across
2,007 tracked objects, including `allcode_011026.zip` (17 MB), two Michelin
CSVs (22 MB), `allcode.jar`, a fully committed `.venv/` with compiled
binaries, and — see §11 — a tracked Google service-account key. The media
pipeline gets a clean repository.

```
mediamixer/
├── README.md
├── requirements.txt              # boto3, psycopg2
├── requirements-dev.txt          # pytest, moto[s3]
├── sql/
│   ├── 001_content_library_schema.sql
│   └── 002_cities_reference.sql
├── migration/
│   ├── ExportFromServer2.sh
│   └── SeedMediaDatabase.py
├── S3Interpreter.py
├── PostgresInterpreter.py        # vendored, 109 lines
├── ContentLibraryPaths.py
├── ContentLibraryProbe.py
├── ContentLibrarySync.py
├── api/                          # FastAPI service, server 3 (see §8)
│   ├── main.py                   # content library endpoints
│   └── db.py                     # single engine, media database
├── deploy/
│   ├── mediamixer-api.service
│   ├── mediamixer-sync.service
│   └── mediamixer-sync.timer
└── tests/
    ├── conftest.py
    ├── fixtures/
    │   ├── s3_listing_2026-07-25.json
    │   └── ffprobe/*.json
    ├── test_content_library_paths.py
    ├── test_content_library_probe.py
    └── test_content_library_sync.py
```

`PostgresInterpreter.py` is copied rather than imported across repositories —
it is 109 lines and a cross-repo dependency is not worth the coupling.

### One repository, not two

The API and the pipeline stay in a single repository. They share the schema,
so a migration changes what the sync writes and what the API returns in the
same commit; splitting them means coordinating that across two pull requests
and hoping deployments stay in step. They also share `S3Interpreter.py`,
`ContentLibraryPaths.py`, and the database layer, which would otherwise have
to be duplicated or extracted into a versioned third package.

Repository boundaries are not deployment boundaries. The two systemd units
in `deploy/` start, stop, and restart independently from one checkout, and
if render workers later move to their own instance that is the same
repository deployed to a second machine with a different unit enabled.

Across all the code, three repositories are involved and only one is new:

| Repository | Role | Change |
|---|---|---|
| `mediamixer` | Everything on server 3; owns the `content_library_*` schema going forward | New |
| `newguide/backend` | Admin backend on server 2 | ~30 lines of proxy (§8) |
| `genAITest` | Build tooling | None — retains the existing schema files as the record of what server 2 has |

`ContentLibraryPaths.py` (key → asset type, city, category, emotion) and
`ContentLibraryProbe.py` (ffprobe JSON → typed fields; streaming SHA-256) are
separate from the `ContentLibrarySync.py` entrypoint specifically so the
tests can exercise them with no S3, no database, and no `ffprobe`.

This repository owns the `content_library_*` schema going forward.
`genAITest` keeps `create_content_library_table.sql` and
`alter_content_library_add_hook.sql` as the accurate, permanent record of
what server 2 has.

---

## 4. Data migration: server 2 → server 3

**The governing rule: copy, never move.** Server 2 is read once. Nothing is
written, altered, or dropped there — including the original
`content_library_assets` table, which stays in place as frozen history. It
costs nothing to leave and deleting it would mean touching the golden source
for no benefit.

Two things move: the content library rows, and a cities reference subset.
Everything else on server 2 stays where it is.

### 4.1 What is actually irreplaceable

The technical columns can all be regenerated by re-running inventory against
S3. What cannot be regenerated is the **human tagging**: `place_name`,
`hook_compatibility`, `notes`, `type`, `subtype`, `category`, `subcategory`,
`cityid`, `country`, and the assigned `asset_id`. That is the payload of this
migration; everything else is convenience.

### 4.2 Export from server 2 (read-only)

Run on server 2 over the localhost socket. `pg_dump` of a single table takes
only an `ACCESS SHARE` lock, which does not block reads or writes — the admin
UI keeps working throughout.

Take **two** exports. CSV is the one that gets loaded, because it is
inspectable and diffable; the SQL dump is an independent second copy in case
the CSV round-trip surprises you.

```bash
STAMP=$(date +%Y%m%d)

# 1. CSV — the copy that gets loaded
psql -h localhost -U bigcity -d <golden_db> -c "\copy ( \
  SELECT id, asset_id, s3_key, filename, folder, place_name, cityid, \
         country, type, subtype, category, subcategory, \
         hook_compatibility, notes, duration, size_bytes, content_type, \
         created_at, updated_at \
  FROM public.content_library_assets ORDER BY id \
) TO 'content_library_assets_${STAMP}.csv' WITH CSV HEADER"

# 2. Independent second copy, schema-drift tolerant
pg_dump -h localhost -U bigcity -d <golden_db> \
  --table=public.content_library_assets --data-only --column-inserts \
  -f content_library_assets_${STAMP}.sql

# 3. Facts to verify against after loading
psql -h localhost -U bigcity -d <golden_db> -At -c \
  "SELECT count(*), count(place_name), count(hook_compatibility), \
          count(notes), max(id) FROM public.content_library_assets"
```

`--column-inserts` emits one `INSERT` per row naming its columns, which
tolerates the target's extra columns. It is slow on large tables and
irrelevant here — this table is small.

Also export the cities subset. Column names are confirmed at export time;
`main.py:3262` establishes that `cities` has at least `cityid` and `country`.

```bash
psql -h localhost -U bigcity -d <golden_db> -c "\copy ( \
  SELECT cityid, country FROM public.cities WHERE cityid IS NOT NULL ORDER BY cityid \
) TO 'cities_reference_${STAMP}.csv' WITH CSV HEADER"
```

### 4.3 Transfer

Workstation-mediated, in both hops, reusing the `scp` pattern that
`SendDBsToAWS.py` already uses in the other direction:

```
server 2  ──scp──▶  workstation  ──scp──▶  server 3
```

Deliberately **not** server 2 → server 3 directly. A direct copy needs
connectivity between the two boxes, which is exactly what this architecture
avoids. Verify with `sha256sum` at each hop, the way `SendDBsToAWS.py`
already does.

### 4.4 Load into server 3

Create the schema first (§5), then load through a staging table rather than
directly, so a malformed CSV cannot half-populate the real one.

```sql
BEGIN;

CREATE TEMP TABLE staging_cl_import (
    id BIGINT, asset_id TEXT, s3_key TEXT, filename TEXT, folder TEXT,
    place_name TEXT, cityid TEXT, country TEXT, type TEXT, subtype TEXT,
    category TEXT, subcategory TEXT, hook_compatibility TEXT[], notes TEXT,
    duration TEXT, size_bytes BIGINT, content_type TEXT,
    created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ
);

\copy staging_cl_import FROM 'content_library_assets_YYYYMMDD.csv' WITH CSV HEADER

INSERT INTO public.content_library_assets (
    id, asset_id, bucket_name, s3_key, filename, folder, place_name,
    cityid, country, type, asset_type, subtype, category, subcategory,
    hook_compatibility, notes, duration, size_bytes, content_type,
    status, rights_status, first_seen_at, last_seen_at,
    created_at, updated_at
)
SELECT
    s.id, s.asset_id, 'big-city-travel-guide-clips', s.s3_key, s.filename,
    s.folder, s.place_name, s.cityid, s.country,
    s.type,                                    -- legacy column preserved
    CASE lower(btrim(s.type))                  -- normalized alongside it
        WHEN 'b-roll' THEN 'broll'
        WHEN 'broll'  THEN 'broll'
        WHEN 'app'    THEN 'app'
        WHEN 'reaction' THEN 'reaction'
        WHEN 'music'  THEN 'music'
        WHEN 'voiceover' THEN 'voiceover'
        WHEN 'caption' THEN 'caption'
        WHEN 'cta'    THEN 'cta'
        ELSE NULLIF(lower(btrim(s.type)), '')
    END,
    s.subtype, s.category, s.subcategory, s.hook_compatibility, s.notes,
    s.duration, s.size_bytes, s.content_type,
    'discovered',                              -- see §4.5
    'unknown',
    COALESCE(s.created_at, now()), now(),
    COALESCE(s.created_at, now()), COALESCE(s.updated_at, now())
FROM staging_cl_import s;

-- Required: id values were carried over explicitly, so the sequence is stale.
SELECT setval(
    pg_get_serial_sequence('public.content_library_assets', 'id'),
    COALESCE((SELECT max(id) FROM public.content_library_assets), 1)
);

COMMIT;
```

Two details that matter:

**Original `id` values are preserved.** `asset_id` is derived from `id`
(`'UGC-' || lpad(id::text, 5, '0')`, `main.py:3296`). Letting ids be
reassigned would leave `UGC-00007` sitting on row 12, which is confusing
forever. Preserving them is free because nothing references these rows yet.

**The sequence must be reset.** This is the classic follow-on bug: carrying
explicit ids leaves the sequence at 1, and the next insert fails on a
duplicate key. The `setval` above is not optional.

The cities reference loads the same way into `cities_reference` (§5), named
to signal that it is a lagging read-only copy and not authoritative.

### 4.5 Imported rows are not `active`

Every imported row lands in `status='discovered'` with
`rights_status='unknown'`, regardless of how complete its tagging is.

The human semantics are trustworthy; the technical facts do not exist yet.
No imported row has `duration_ms`, dimensions, orientation, codecs, or a
checksum, and none has a rights decision on record. Design principle 8 is
fail-closed: unreviewed and rights-unknown assets are ineligible. So the
import feeds the normal path — inventory confirms the object still exists,
probe fills the technical fields, the row moves to `needs_review`, and a
person clears rights before it reaches `active`. The migration carries the
tagging forward; it does not shortcut the review gate.

### 4.6 Verification before cutover

- Row count on server 3 equals the count captured in §4.2.
- `count(place_name)`, `count(hook_compatibility)`, `count(notes)` all match —
  this is the check that the irreplaceable data survived.
- `max(id)` matches, and the sequence is greater than it.
- `asset_id` is unique and non-null wherever it was non-null at source.
- Every non-null `hook_compatibility` is a `TEXT[]` with the same element
  count as source; spot-check several by hand. Array round-tripping through
  CSV is the most likely place for a silent corruption.
- Spot-check ten fully tagged rows field by field against server 2.
- `cityid` values not present in `cities_reference` are reported (not
  rejected — see §5).

### 4.7 Rollback

There is nothing to roll back on server 2, because nothing was written
there. If the load goes wrong on server 3: truncate and reload from the CSV.
The authoritative copy remains on the golden source, untouched, for as long
as you leave it there.

---

## 5. Schema on server 3

`sql/001_content_library_schema.sql` — the design package's
`sql/001_content_library_v2.sql` restated as a fresh `CREATE`, since there is
no legacy table to alter:

- All v2 columns present from the start; constraints created **valid**, not
  `NOT VALID`, because the table is empty when they are applied.
- Legacy `type`, `duration`, and `hook_compatibility TEXT[]` are still
  carried, so the existing `ContentLibrary.tsx` tab keeps working unchanged.
  The normalized `content_library_tags` / `_hooks` tables are added
  alongside them, not instead of them.
- `content_library_renders`, `_render_assets`, `_render_artifacts`,
  `_asset_enrichments` as specified in the design package, including the
  `CHECK (s3_key LIKE 'ugc-assets/exported/%')` guard on artifacts.
- **New:** `content_library_inventory_runs` — run id, started/completed
  timestamps, a `listing_complete BOOLEAN`, and the reconciliation counters
  from `05-ingestion-workflow.md` step 8. The design package assumes an
  inventory-run id but never defines storage for it, and without persisted
  `listing_complete` the "never mark missing on an incomplete listing" rule
  cannot survive a process restart.

`sql/002_cities_reference.sql` — `cities_reference (cityid TEXT PRIMARY KEY,
country TEXT, refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now())`.

**No foreign key from `content_library_assets.cityid` to `cities_reference`.**
It is a lagging copy; a hard FK would reject a legitimate asset whose city was
added on server 2 but not yet copied across. Instead the sync emits an
unresolved-`cityid` report. Fail soft on a replicated reference, fail closed
on rights and review.

---

## 6. Inventory

**Listing.** Paginate `ugc-assets/`. Skip keys ending `/` and keys with
`Size == 0` — the zero-byte folder markers. Skip everything under
`ugc-assets/exported/` unless `--allow-exported-reimport` is passed. Capture
bucket, key, size, ETag, `VersionId` when present, `LastModified`.

**Idempotency.** Upsert on `(bucket_name, s3_key)`. `first_seen_at` written
once, `last_seen_at` every run. Re-running changes no row count and no
`first_seen_at`.

**Reconciliation.** Runs only after a listing that completed without
exception. A paginator failure aborts reconciliation entirely — a partial
listing must never mark the library missing. Rows are never deleted;
`status` moves to `missing` with `missing_since`.

**S3 is read-only, structurally.** `S3Interpreter.py` exposes only `list`,
`head`, and `presign`. No `put_object`, `copy_object`, or `delete_object`
exists in the module, so no code path can reach a write API by accident.

**Probe.** `ffprobe` against a presigned URL, per the existing backend
convention — nothing written to disk. Records `duration_ms`, `width`,
`height`, rotation-corrected `orientation`, `frame_rate`, codecs,
`has_audio`, and the raw JSON into `probe_data JSONB`. Legacy `"m:ss"`
`duration` continues to be written for UI compatibility.

**Checksum.** SHA-256 streamed in chunks — never `body.read()` into memory.
Computed once and skipped on later runs when ETag and size are unchanged.
This is the only operation that reads whole objects: 658 MiB once.

---

## 7. Reaction aliases

73 objects, 59 unique payloads: 31 reaction objects collapse to 17
performances, so 14 are byte-identical duplicates of another key. This is
**intentional classification** — a clip filed under both `surprised/` and
`shocked/` is being labeled compatible with both.

1. Register all 31 keys. Every key keeps its row and its S3 lineage.
2. Group by `checksum_sha256` after probing. ETag is a hint only; multipart
   ETags are not content hashes.
3. Lowest `id` in a group is canonical; the others get
   `duplicate_of_asset_id` pointing at it.
4. **Union the folder-derived emotion tags onto the canonical row**, so a
   query for either emotion finds the one performance.
5. Selection filters `duplicate_of_asset_id IS NULL` — which is why the
   partial index carries that predicate. One performance, one editorial
   weight, never twice in a render.

Acceptance: 73 rows, 59 distinct checksums, 14 rows with
`duplicate_of_asset_id` set, every emotion folder represented in canonical
rows' tags.

---

## 8. Backend split and UI cutover

Once server 3 is seeded, further tagging through the existing UI would land
on server 2's copy and silently diverge. Seed and cut over close together;
after that, server 2's table is frozen history nobody reads.

**The front end does not change.** `ContentLibrary.tsx` already works and is
one tab among many in a single admin tool. A second front end would mean two
URLs and duplicated shell, nav, and styling for no gain.

**The content library backend moves to server 3**, reached through a thin
proxy in server 2's existing admin backend.

Two facts drive that shape rather than pointing the browser directly at
server 3:

- `admin_ui/src/api.ts` sets `baseURL: ""` — every admin request is a
  same-origin relative path, because the UI and backend share a host on
  server 2. A direct call to server 3 becomes cross-origin and needs CORS
  middleware, which `main.py` has none of today.
- **No admin route is authenticated.** `ADMIN_SECRET` (`main.py:654`) is
  used only as an outbound `X-Admin-Secret` header when calling the
  concierge API; nothing validates it inbound. The admin tool is protected
  by not being publicly reachable, not by auth. Exposing server 3's API
  directly to the browser would put an unauthenticated API in front of the
  media database on a new network surface.

Proxying keeps server 3 on the private network, reachable only from server 2.
The codebase already has this exact pattern: `admin_concierge_plan`
(`main.py:3336`) forwards admin requests to a remote EC2 API with `httpx`,
including the `X-Admin-Secret` header. The Content Library endpoints become
the same kind of passthrough — roughly thirty lines, and the tab keeps
rendering whatever the response contains.

Server 3's backend should validate `X-Admin-Secret` inbound, which is
marginally better than what exists today and costs nothing to add while the
service is being written.

`_CL_EDITABLE` (`main.py:3171`) gains `status`, `rights_status`,
`rights_source`, and `city_agnostic` so activation happens through the
existing tab rather than requiring new UI in this phase.

---

## 9. Dry-run and tests

`--dry-run` follows the house convention: every read-only step runs for real
— listing, classification, `ffprobe`, SHA-256 — and nothing is written. The
full reconciliation report prints with the `"Would be "` prefix
(`ClassifyFoodShops.py:420`). This is what verifies the 73/59 counts against
live S3 before anything is committed.

Tests run with no database, no AWS credentials, and no `ffprobe`.

**`test_content_library_paths.py`** — pure functions over the real key
shapes: `app/new-york/guide/…` → `app`/`new-york`/`guide`;
`b-roll/food/pizza/new-york/…` → `broll`/`food`/`pizza`;
`reactions/surprised/…` → `reaction`/`surprised`. Plus the documented
exceptions: `app_newyork_*` filenames under `new-york` directories,
`b-roll/food/fancy/`, and reaction filenames with spaces and repeated
underscores.

**`test_content_library_probe.py`** — recorded `ffprobe` JSON parsed into
typed fields. Includes the rotation case: a 1920×1080 stream with
`rotate=90` is `portrait`. Getting that wrong silently admits sideways
footage into a 9:16 render.

**`test_content_library_sync.py`** — `moto` provides in-process S3 with a
fixture reproducing the 2026-07-25 snapshot. Asserts: 73 objects registered
(6 app, 36 broll, 31 reaction); zero folder markers; `exported/` excluded;
59 distinct checksums with 14 aliases; emotion tags unioned onto canonical
rows; a second run changes no row count and no `first_seen_at`; an exception
mid-pagination marks nothing missing; `--dry-run` writes nothing; and no S3
write API is ever called.

---

## 10. First three test videos

Rendering is Phase 5 and stays gated behind everything above — the README's
precondition is that the catalog can first distinguish active,
review-required, missing, and rejected assets.

**Which three.** All New York, since 40 of 73 assets are New York and food
alone has 31 clips: a pizza brief, a bagel brief, and a landmark brief. A
**fourth, negative** case runs alongside — a Tokyo destination brief must
return `insufficient_assets` rather than borrowing New York footage. That
negative case proves the city-leakage guard works and is worth more than the
three successes.

**Safety controls.**

- `environment=dev` only; output to
  `ugc-assets/exported/dev/{yyyy}/{mm}/{dd}/{render-id}/`, never `prod`.
- Write access restricted at two levels: an IAM policy on server 3's role
  scoped to `ugc-assets/exported/*`, and the
  `CHECK (s3_key LIKE 'ugc-assets/exported/%')` constraint on
  `content_library_render_artifacts`. Belt and suspenders, because a bug
  writing into `b-roll/` would overwrite an irreplaceable master.
- Every render id is new; a retry is a new directory. Nothing overwrites.
- Sources read-only: resolve by exact `VersionId` where available, verify
  SHA-256 against the catalog before use, and re-list the source prefix
  afterward to confirm no ETag changed.
- Recipe first, render second — the recipe JSON is validated and reviewed by
  a person before any `ffmpeg` invocation.
- Per-render scratch directory with a quota, on a volume separate from the
  database if the database is on server 3, deleted after upload and checksum
  confirmation. Free space is checked as a precondition, so a render refuses
  to start rather than discovering the limit mid-encode.
- Confirm bucket versioning is enabled before the first render.

**Verification per video.** 1080×1920, 30 fps, H.264/AAC, playable; duration
within tolerance; `manifest.json` resolves every source and trim window;
`validation.json` passes; artifact checksums match the manifest; and the
source-prefix ETag comparison shows zero changes.

---

## 11. Two items independent of this project

**A Google service-account key is tracked in git.**
`travel-guides-439701-3e1e33339701.json` is committed to `genAITest`; the
filename matches Google's standard key-export pattern. It was not opened.
Rotate the key in Google Cloud — rotation is what matters, and purging git
history would not undo the exposure. The entire `.venv/` is committed too,
despite `.gitignore` listing it, because gitignore does not untrack files
already added.

**The golden source may have no backups.** `pg_dump` appears nowhere in
either repository — no script, no cron, no scheduled job — and server 2 runs
PostgreSQL on the instance rather than RDS, so there are no automated
snapshots and no point-in-time recovery. If that EBS volume is lost, the
golden source is lost. A backup configured outside these repositories would
not be visible here, so this may already be handled; if it is not, it is a
larger risk than anything the media pipeline introduces.

---

## 12. Sequencing

| Step | Action | Touches |
|---|---|---|
| 0 | Decide §1.1 (RDS vs on-instance for the media database) | nothing |
| 1 | Create the repository; write the four modules and the test suite | new repo |
| 2 | Run the suite locally — green with no DB, no AWS, no `ffprobe` | new repo |
| 3 | Provision server 3 and the media database; IAM role with S3 read on `ugc-assets/`, write only on `ugc-assets/exported/` | server 3 |
| 4 | Apply `001_content_library_schema.sql` and `002_cities_reference.sql` | server 3 DB |
| 5 | Export from server 2 per §4.2 and capture verification facts | **server 2, read only** |
| 6 | Transfer via workstation; load per §4.4; verify per §4.6 | server 3 DB |
| 7 | `ContentLibrarySync.py --dry-run` against live S3 from server 3; confirm 73/59 | S3 read only |
| 8 | Live sync; probe; checksum; alias merge; reconciliation report | server 3 DB, S3 read only |
| 9 | Admin UI cutover per §8 | backend + UI |
| 10 | Review and activate assets; Phase 5 planning begins | server 3 DB |

Steps 1 and 2 are entirely inside the new repository and can begin
immediately. Server 2 is read at step 5 and never again.
