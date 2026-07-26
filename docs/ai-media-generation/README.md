# Big City Travel Guide — AI Media Generation Design Package

Version: 0.2  
Inventory snapshot: 2026-07-25 (live S3 listing)  
S3 bucket: `s3://big-city-travel-guide-clips/`  
Managed prefix: `ugc-assets/`

## Purpose

This package turns the current clip library and `public.content_library_assets` table into an implementation-ready design for:

1. registering media without changing existing S3 keys;
2. enriching and reviewing metadata;
3. selecting compatible clips for a video brief;
4. assembling vertical social videos reproducibly;
5. writing every generated media artifact beneath `ugc-assets/exported/`; and
6. handing the work to a VS Code implementation agent in bounded phases.

## Documents

| File | Purpose |
|---|---|
| `00-project-overview.md` | Scope, architecture, principles, and success criteria |
| `01-current-inventory.md` | Evidence-based summary of `clips_output.txt` |
| `02-canonical-s3-layout.md` | Layout that preserves all current keys |
| `03-metadata-data-model.md` | Review of the current PostgreSQL model |
| `04-asset-standards.md` | Naming, vocabulary, tags, and quality rules |
| `05-ingestion-workflow.md` | Inventory, probe, enrich, review, and activation |
| `06-video-assembly-workflow.md` | Brief-to-render workflow and selection rules |
| `07-output-export-conventions.md` | Required generated-media destinations and manifests |
| `08-vscode-implementation-plan.md` | Phased build plan and acceptance criteria |
| `09-repo-implementation-plan.md` | Repository-specific plan: server 3, data migration, conventions |
| `10-ec2-server3-setup.md` | EC2 configuration for server 3: instance, key pair, IAM, security groups |
| `sql/001_content_library_v2.sql` | Recommended forward migration |
| `sql/002_inventory_upsert_example.sql` | Safe inventory registration pattern |

## Authoritative decisions

- Existing source objects remain at their current keys; no bulk S3 move is required.
- The database, not folder names alone, is the searchable catalog.
- Raw `.mov` files are accepted and preserved.
- Technical media properties are measured by a probe, not entered manually.
- AI-enriched metadata is provisional until it passes validation/review.
- Generated media must be stored under `ugc-assets/exported/`.
- Reaction clips are present: 31 objects across five folders, representing 17 unique
  payloads after checksum/ETag deduplication.
- Each render has a database record, immutable recipe, and JSON manifest.

## Recommended first implementation slice

Build an inventory sync command that reads S3 objects, probes media, upserts assets by `(bucket_name, s3_key)`, marks missing objects without deleting rows, and produces a reconciliation report. Do not start automated video generation until the catalog can distinguish active, review-required, missing, and rejected assets.
