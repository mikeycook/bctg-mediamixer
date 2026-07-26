#!/usr/bin/env bash
#
# ExportFromServer2.sh
#
# RUNS ON SERVER 2 — the golden source. Read-only.
#
# Every statement here is a SELECT or a pg_dump. Nothing is written,
# altered, or dropped. The original content_library_assets table stays
# exactly where it is and keeps working; this only takes a copy.
#
# pg_dump of a single table takes an ACCESS SHARE lock, which does not
# block reads or writes, so the admin Content Library tab keeps working
# throughout.
#
# Two exports of the assets are taken. The CSV is what gets loaded, because
# it is inspectable and diffable. The SQL dump is an independent second
# copy in case the CSV round-trip surprises us — array and timestamp
# columns are the usual culprits.
#
# Usage:
#     ./ExportFromServer2.sh
#     ./ExportFromServer2.sh /var/tmp/exports
#
# Env:
#     DATABASE_URL  — required; the golden source on server 2
#
# Afterwards, copy the output directory to your workstation and then to
# server 3. Do not copy server 2 to server 3 directly: the two have no
# network path to each other, and that is deliberate.

set -euo pipefail

OUTDIR="${1:-$HOME/mediamixer-export-$(date +%Y%m%d)}"
STAMP="$(date +%Y%m%d)"

: "${DATABASE_URL:?Set DATABASE_URL to the server 2 database}"

mkdir -p "$OUTDIR"
cd "$OUTDIR"

echo "[EXPORT] target: $OUTDIR"
echo "[EXPORT] server 2 is read-only throughout; nothing here writes to it"
echo

# ---------------------------------------------------------------------------
# 1. Content library assets — CSV, the copy that gets loaded
# ---------------------------------------------------------------------------
echo "[1/4] content_library_assets -> CSV"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "\copy ( \
  SELECT id, asset_id, s3_key, filename, folder, place_name, cityid, \
         country, type, subtype, category, subcategory, \
         hook_compatibility, notes, duration, size_bytes, content_type, \
         created_at, updated_at \
  FROM public.content_library_assets ORDER BY id \
) TO 'content_library_assets_${STAMP}.csv' WITH CSV HEADER"

# ---------------------------------------------------------------------------
# 2. Content library assets — independent second copy
# ---------------------------------------------------------------------------
echo "[2/4] content_library_assets -> SQL dump"
pg_dump "$DATABASE_URL" \
    --table=public.content_library_assets \
    --data-only --column-inserts \
    -f "content_library_assets_${STAMP}.sql"

# ---------------------------------------------------------------------------
# 3. Cities reference
#
# cityid is the canonical 'CIT-...' identifier; city_slug is derived from
# cityname so that path-derived slugs ('new-york') can resolve to it.
# ---------------------------------------------------------------------------
# The slug expression must stay identical to ContentLibraryPaths.slugify().
# Resolution joins a slug derived here against one derived from an S3 path,
# and a divergence would silently fail to match rather than raising.
echo "[3/4] cities -> CSV"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "\copy ( \
  SELECT cityid, cityname, country, \
         btrim(regexp_replace(lower(btrim(cityname)), '[^a-z0-9]+', '-', 'g'), '-') AS city_slug \
  FROM public.cities WHERE cityid IS NOT NULL ORDER BY cityid \
) TO 'cities_reference_${STAMP}.csv' WITH CSV HEADER"

# ---------------------------------------------------------------------------
# 4. Verification facts — compared against server 3 after loading
# ---------------------------------------------------------------------------
echo "[4/4] verification facts"
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -A -F'|' -c "
SELECT
    count(*)                        AS rows,
    count(asset_id)                 AS with_asset_id,
    count(place_name)               AS with_place_name,
    count(hook_compatibility)       AS with_hooks,
    count(notes)                    AS with_notes,
    count(cityid)                   AS with_cityid,
    coalesce(max(id), 0)            AS max_id
FROM public.content_library_assets" | tee "verification_${STAMP}.txt"

echo
echo "[EXPORT] done. Files in $OUTDIR:"
ls -la
echo
echo "Next: copy this directory to your workstation, then to server 3, then run"
echo "      SeedMediaDatabase.py against it. Server 2 needs nothing further."
