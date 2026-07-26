# MediaMixer

AI media generation pipeline for Big City Travel Guide. Inventories the UGC
clip library in S3, enriches and reviews its metadata, and — later — assembles
vertical social videos from it.

**Read `docs/ai-media-generation/09-repo-implementation-plan.md` first.** It is
the authoritative plan. `10-ec2-server3-setup.md` covers the infrastructure.
Documents `00`–`08` are the original design package.

## Where things run

| | Role | This project's relationship to it |
|---|---|---|
| Server 1 | API services + user settings database (favorites) | Untouched, never in scope |
| Server 2 | Admin backend + UX, and the **golden source** build database | **Never modified.** Read exactly once, to copy one table. Gains ~30 lines of proxy code |
| Server 3 | This code: inventory sync, content library API, later the render worker | `c7i.large`, Ubuntu 24.04 x86_64, `us-east-1c` |
| RDS `mediamixerdb` | The `mediamixer` database | `db.t4g.micro`, 20 GB gp3, Single-AZ, `us-east-1c`, private |

Server 3 has **no network path to server 2**. It reaches S3 through its IAM
instance role and its own database privately. The cities reference is seeded
from an export, not queried live.

## Decisions already made

- **Copy, never move.** Server 2's `content_library_assets` is exported once
  and left in place as frozen history. No migration ever runs there.
- **Fresh CREATE, not ALTER.** `sql/001_content_library_schema.sql` targets an
  empty database, so constraints are VALID from the start.
- **Legacy columns retained** (`type`, `duration`, `hook_compatibility TEXT[]`)
  so the existing admin Content Library tab keeps working unchanged.
- **One repository**, not two. The API and pipeline share the schema and
  modules. They deploy as two systemd units from one checkout.
- **Master DB user is `bigcity`** — the schema files say `OWNER TO bigcity`
  nine times; any other name breaks them.
- **Imported rows land in `status='discovered'`, `rights_status='unknown'`.**
  The import carries tagging forward; it does not shortcut the review gate.
- Admin UI is unchanged; server 2's backend proxies to server 3.

## Non-negotiables

1. **Never write to a source S3 prefix.** Generated media goes only under
   `ugc-assets/exported/`. Enforced twice: the IAM policy on server 3, and
   `CHECK (s3_key LIKE 'ugc-assets/exported/%')` on render artifacts.
2. **`S3Interpreter.py` exposes only `list`, `head`, `presign`.** No write API
   exists in the module, so no code path can reach one by accident.
3. **Never mark assets missing after an incomplete S3 listing.** Reconciliation
   reads `listing_complete` from `content_library_inventory_runs`.
4. **Fail closed.** Unreviewed, rights-unknown, missing, or wrong-city assets
   are ineligible for selection. Never substitute across cities.
5. **Probe over presigned URL; checksum streamed in chunks.** Nothing is
   written to disk during inventory — deliberate, because `ffmpeg` scratch and
   disk pressure are a real risk on this box later.
6. **No AWS credentials anywhere.** The instance role is the mechanism. Never
   run `aws configure` on server 3.

## Conventions

Inherited from `genAITest`, which this code sits alongside:

- Flat PascalCase modules; `*Interpreter.py` wraps one external system.
- Scripts: `#!/usr/bin/env python3`, long docstring with `Usage:` and `Env:`
  sections, `argparse` with `--dry-run`, `parse_database_url()` feeding
  `PostgresInterpreter`, bracketed `print()` logging, `raise SystemExit(...)`
  for fatal misconfiguration, `"Would be "` prefix on dry-run summaries.
- SQL: hand-written idempotent files applied with `psql`. No migration
  framework. `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`,
  `ALTER TABLE ... OWNER TO bigcity`.
- Config from environment: `/etc/mediamixer/mediamixer.env`, `chmod 600`,
  loaded by systemd. Never committed.

Python 3.12 (Ubuntu 24.04). Tests must run with no database, no AWS
credentials, and no `ffprobe`.

## Acceptance target

Against the 2026-07-25 S3 snapshot: **73 objects** registered — 6 app, 36
b-roll, 31 reaction — collapsing to **59 unique payloads**, with 14 rows
carrying `duplicate_of_asset_id`. No folder markers registered, nothing from
`ugc-assets/exported/` ingested, and a second run changing no row count and no
`first_seen_at`.

## Status — Phases 1 and 2 complete

**Infrastructure.** Server 3 is `c7i.large`, Ubuntu 24.04 x86_64, `us-east-1c`,
in the existing VPC. Its IAM boundary is proven in both directions: reads
succeed, and a write to `ugc-assets/b-roll/` is refused with an explicit deny.
The database is RDS `mediamixerdb`, PostgreSQL 16, `db.t4g.micro`, 20 GB gp3,
Single-AZ, private, reachable only from server 3's security group. Schema
files `001`, `002` and `003` are applied. Config lives in
`/etc/mediamixer/mediamixer.env`; the venv is at `/opt/mediamixer/venv` and
the checkout at `/opt/mediamixer/app`.

**Migration.** Server 2 was read once and never written to. 73 tagged assets
and 892 cities were copied across, verified field by field: 42 `place_name`,
34 `hook_compatibility`, 42 `notes`, 73 `asset_id`, ids preserved, sequence
reset. Its original table remains in place as frozen history.

**Inventory.** A full live run registered all 73 objects — 6 app, 36 b-roll,
31 reaction — with no duplicates against the seeded rows. All 73 are probed
and checksummed. 14 byte-identical aliases are marked, leaving 59 unique
payloads, and 31 folder-derived emotion tags are merged onto canonical rows.
Every row sits at `status='needs_review'`; nothing is `active`.

Code is on GitHub at `mikeycook/bctg-mediamixer`; server 3 pulls via a
read-only deploy key. 107 tests pass with no database, AWS, or ffprobe.

## Next

1. **Admin UI cutover (urgent — see §8 of the implementation plan).** The
   Content Library tab still reads and writes server 2's now-frozen copy.
   Any tagging done there diverges silently from server 3. Server 2's backend
   should proxy these endpoints to a small API on server 3.
2. `deploy/` systemd units — `mediamixer-api.service`, `mediamixer-sync`
   service and timer. Not yet written.
3. Phase 3: review workflow and rights fields, so assets can reach `active`.
   Nothing is eligible for selection until they do.
