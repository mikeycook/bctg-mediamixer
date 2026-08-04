#!/usr/bin/env python3
"""
ContentLibrarySync.py

Registers the UGC clip library in s3://big-city-travel-guide-clips/ into the
mediamixer content library, measures each object with ffprobe, fingerprints
it with SHA-256, and reconciles objects that have disappeared.

Source objects are never moved, renamed, deleted, or overwritten. The S3
wrapper this uses has no write method at all, so no code path here can reach
one.

What a run does:

  1. Opens an inventory run and records its ID.
  2. Lists ugc-assets/, skipping zero-byte folder markers, non-media
     objects, and everything under ugc-assets/exported/.
  3. Proposes asset type, city, category and emotion from the key. Those are
     path-derived proposals; they fill blanks but never overwrite a value a
     reviewer has set.
  4. Upserts on (bucket_name, s3_key). If an object's ETag changed, its
     probe data and checksum are invalidated so they get recomputed.
  5. Probes and checksums anything missing them.
  6. Marks previously known but unseen objects 'missing' — but ONLY if the
     listing completed. A partial listing that condemned the live library
     would be the worst thing this script could do.
  7. Groups byte-identical objects by checksum, marks the non-canonical ones
     as duplicates, and merges their folder-derived emotion tags onto the
     canonical row.

Nothing reaches 'active' here. Probed rows land in 'needs_review' and a
person clears rights before they are eligible for anything.

Usage:
    python3 ContentLibrarySync.py --dry-run
    python3 ContentLibrarySync.py
    python3 ContentLibrarySync.py --limit 5 --dry-run
    python3 ContentLibrarySync.py --no-checksum
    python3 ContentLibrarySync.py --reprobe

Env (from the systemd EnvironmentFile in the .service, or ./.env locally):
    DATABASE_URL  — required
    CLIPS_BUCKET  — default big-city-travel-guide-clips
    CLIPS_PREFIX  — default ugc-assets/
    CLIPS_REGION  — default us-east-1
"""

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import ContentLibraryPaths as clpaths
import ContentLibraryProbe as clprobe
from PostgresInterpreter import PostgresInterpreter
from S3Interpreter import S3Interpreter

# Legacy display values, so rows created here look right in the existing
# admin Content Library tab alongside hand-entered ones.
LEGACY_TYPE_DISPLAY = {
    "app": "App",
    "broll": "B-Roll",
    "reaction": "Reaction",
    "music": "Music",
    "voiceover": "Voiceover",
    "caption": "Caption",
    "cta": "CTA",
}


def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_database_url(database_url):
    database_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    u = urlparse(database_url)
    if u.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"Unsupported DB scheme: {u.scheme}")
    user = u.username or ""
    password = unquote(u.password or "")
    host = u.hostname or "127.0.0.1"
    port = str(u.port or 5432)
    database = (u.path or "").lstrip("/")
    if not user or not database:
        raise ValueError(f"Could not parse user/database from DATABASE_URL: {database_url}")
    return {"user": user, "password": password, "host": host, "port": port,
            "database": database}


def execute_returning(db, sql, params):
    """
    PostgresInterpreter commits only when a statement returns no rows, so an
    upsert with RETURNING would otherwise stay uncommitted. Commit here.
    """
    rows = db.execute_query(sql, params)
    if rows is False:
        return None
    db.connection.commit()
    return rows


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

def list_source_objects(s3, prefix, allow_exported=False, limit=None):
    """
    Returns (objects, listing_complete, counters).

    listing_complete is the important value. It is False if pagination
    raised, and reconciliation must refuse to run when it is False.
    """
    objects = []
    counters = {"listed": 0, "markers": 0, "exported": 0, "non_media": 0,
                "images": 0}
    complete = False
    try:
        for obj in s3.list_objects(prefix):
            counters["listed"] += 1
            key, size = obj["key"], obj["size"]

            if clpaths.is_folder_marker(key, size):
                counters["markers"] += 1
                continue
            if clpaths.is_exported(key) and not allow_exported:
                counters["exported"] += 1
                continue
            # The still-image library lives under ugc-assets/images/ and is
            # owned by ImageLibrarySync — the video sync must not ingest it.
            if key.startswith("ugc-assets/images/"):
                counters["images"] += 1
                continue
            if not clpaths.is_media(key):
                counters["non_media"] += 1
                continue

            objects.append(obj)
            if limit and len(objects) >= limit:
                # A truncated run is not a complete listing, so it must not
                # be allowed to mark anything missing.
                return objects, False, counters
        complete = True
    except Exception as exc:
        print(f"[ERROR] S3 listing failed after {counters['listed']} objects: {exc}")
    return objects, complete, counters


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

UPSERT_SQL = """
INSERT INTO public.content_library_assets (
    bucket_name, s3_key, filename, folder, size_bytes, content_type,
    etag, s3_last_modified_at,
    asset_type, type, category, subcategory, city_slug,
    status, first_seen_at, last_seen_at, missing_since
) VALUES (
    %(bucket)s, %(key)s, %(filename)s, %(folder)s, %(size)s, %(content_type)s,
    %(etag)s, %(last_modified)s,
    %(asset_type)s, %(legacy_type)s, %(category)s, %(subcategory)s, %(city_slug)s,
    'discovered', %(seen_at)s, %(seen_at)s, NULL
)
ON CONFLICT (bucket_name, s3_key) DO UPDATE SET
    filename            = EXCLUDED.filename,
    folder              = EXCLUDED.folder,
    size_bytes          = EXCLUDED.size_bytes,
    content_type        = EXCLUDED.content_type,
    etag                = EXCLUDED.etag,
    s3_last_modified_at = EXCLUDED.s3_last_modified_at,
    last_seen_at        = EXCLUDED.last_seen_at,
    missing_since       = NULL,
    -- Path-derived values are proposals. They fill a blank; they never
    -- overwrite something a reviewer has already set.
    asset_type  = COALESCE(public.content_library_assets.asset_type,  EXCLUDED.asset_type),
    type        = COALESCE(public.content_library_assets.type,        EXCLUDED.type),
    category    = COALESCE(public.content_library_assets.category,    EXCLUDED.category),
    subcategory = COALESCE(public.content_library_assets.subcategory, EXCLUDED.subcategory),
    city_slug   = COALESCE(public.content_library_assets.city_slug,   EXCLUDED.city_slug),
    -- A changed ETag means the bytes changed, so anything measured from
    -- them is stale and must be recomputed rather than trusted.
    checksum_sha256 = CASE
        WHEN public.content_library_assets.etag IS DISTINCT FROM EXCLUDED.etag
        THEN NULL ELSE public.content_library_assets.checksum_sha256 END,
    probe_data = CASE
        WHEN public.content_library_assets.etag IS DISTINCT FROM EXCLUDED.etag
        THEN NULL ELSE public.content_library_assets.probe_data END,
    duration_ms = CASE
        WHEN public.content_library_assets.etag IS DISTINCT FROM EXCLUDED.etag
        THEN NULL ELSE public.content_library_assets.duration_ms END,
    -- Replaced bytes mean the tagging may now describe footage that is no
    -- longer there. Re-probing is not enough: a clip whose place_name and
    -- category were confirmed against the old content would otherwise go
    -- straight into a render. Send it back for a look. The etag IS NOT NULL
    -- guard stops rows seeded without one from tripping this on first sync.
    status = CASE
        WHEN public.content_library_assets.etag IS NOT NULL
             AND public.content_library_assets.etag IS DISTINCT FROM EXCLUDED.etag
        THEN 'needs_review'
        WHEN public.content_library_assets.status = 'missing' THEN 'discovered'
        ELSE public.content_library_assets.status END
RETURNING id, (xmax = 0) AS inserted, checksum_sha256, duration_ms, status
"""


def upsert_asset(db, obj, classified, seen_at):
    params = {
        "bucket": obj["bucket"], "key": obj["key"],
        "filename": classified.filename, "folder": classified.folder,
        "size": obj["size"], "content_type": obj.get("content_type"),
        "etag": obj["etag"], "last_modified": obj["last_modified"],
        "asset_type": classified.asset_type,
        "legacy_type": LEGACY_TYPE_DISPLAY.get(classified.asset_type),
        "category": classified.category, "subcategory": classified.subcategory,
        "city_slug": classified.city_slug, "seen_at": seen_at,
    }
    rows = execute_returning(db, UPSERT_SQL, params)
    if not rows:
        return None
    asset_id, inserted, checksum, duration_ms, status = rows[0]
    return {"id": asset_id, "inserted": inserted, "checksum": checksum,
            "duration_ms": duration_ms, "status": status}


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

PROBE_UPDATE_SQL = """
UPDATE public.content_library_assets SET
    duration_ms = %(duration_ms)s, duration = %(duration_display)s,
    width = %(width)s, height = %(height)s, orientation = %(orientation)s,
    frame_rate = %(frame_rate)s, video_codec = %(video_codec)s,
    audio_codec = %(audio_codec)s, has_audio = %(has_audio)s,
    probe_data = %(probe_data)s, probe_error = %(probe_error)s,
    status = %(status)s
WHERE id = %(id)s
"""


def probe_asset(db, s3, asset, key, timeout):
    """
    Probes over a presigned URL so nothing is written to disk.

    A probe failure moves the row to 'error' rather than aborting the run;
    a successful one moves 'discovered' to 'needs_review'. Neither reaches
    'active' — only a person does that.
    """
    import json
    result = clprobe.probe(s3.presign(key), timeout=timeout)
    failed = bool(result["error"])
    status = "error" if failed else (
        "needs_review" if asset["status"] in ("discovered", "probing", "error")
        else asset["status"])

    db.execute_query(PROBE_UPDATE_SQL, {
        "id": asset["id"],
        "duration_ms": result["duration_ms"],
        "duration_display": clprobe.format_duration_display(result["duration_ms"]),
        "width": result["width"], "height": result["height"],
        "orientation": result["orientation"], "frame_rate": result["frame_rate"],
        "video_codec": result["video_codec"], "audio_codec": result["audio_codec"],
        "has_audio": result["has_audio"],
        "probe_data": json.dumps(result["probe_data"]) if result["probe_data"] else None,
        "probe_error": result["error"], "status": status,
    })
    return result


def checksum_asset(db, s3, asset_id, key):
    digest = s3.checksum_sha256(key)
    db.execute_query(
        "UPDATE public.content_library_assets SET checksum_sha256 = %s WHERE id = %s",
        (digest, asset_id))
    return digest


# ---------------------------------------------------------------------------
# Reconciliation and alias merging
# ---------------------------------------------------------------------------

def reconcile_missing(db, bucket, run_started_at):
    rows = execute_returning(db, """
        UPDATE public.content_library_assets
        SET status = 'missing', missing_since = COALESCE(missing_since, now())
        WHERE bucket_name = %(bucket)s
          AND s3_key LIKE 'ugc-assets/%%'
          AND s3_key NOT LIKE 'ugc-assets/exported/%%'
          AND last_seen_at < %(started)s
          AND status NOT IN ('archived', 'rejected', 'missing')
        RETURNING id
    """, {"bucket": bucket, "started": run_started_at})
    return len(rows or [])


def merge_duplicates(db, bucket):
    """
    Byte-identical objects under different keys are intentional: the same
    reaction filed under two emotions means it suits both. Every key keeps
    its row and its S3 lineage; the lowest id in a checksum group becomes
    canonical and the rest point at it.

    Selection filters on duplicate_of_asset_id IS NULL, so one performance
    carries one editorial weight and cannot appear twice in a render.
    """
    rows = execute_returning(db, """
        WITH groups AS (
            SELECT checksum_sha256, min(id) AS canonical_id
            FROM public.content_library_assets
            WHERE checksum_sha256 IS NOT NULL AND bucket_name = %(bucket)s
            GROUP BY checksum_sha256
            HAVING count(*) > 1
        )
        UPDATE public.content_library_assets a
        SET duplicate_of_asset_id = g.canonical_id
        FROM groups g
        WHERE a.checksum_sha256 = g.checksum_sha256
          AND a.id <> g.canonical_id
          AND a.duplicate_of_asset_id IS DISTINCT FROM g.canonical_id
        RETURNING a.id
    """, {"bucket": bucket})
    return len(rows or [])


def merge_emotion_tags(db, emotions_by_key, bucket):
    """
    Unions every folder-derived emotion onto the canonical asset, so a
    query for either emotion finds the one performance.

    Assets whose emotions a reviewer has curated are left alone. Folder
    names are a proposal, and once someone has corrected one — removed a
    wrong emotion, added one the folder does not imply — re-deriving from
    the path every night would silently undo it. Provenance is what tells
    the two apart.
    """
    if not emotions_by_key:
        return 0
    attached = 0
    for key, emotions in emotions_by_key.items():
        rows = db.execute_query("""
            SELECT COALESCE(duplicate_of_asset_id, id)
            FROM public.content_library_assets
            WHERE bucket_name = %s AND s3_key = %s
        """, (bucket, key))
        if not rows:
            continue
        canonical_id = rows[0][0]

        curated = db.execute_query("""
            SELECT 1 FROM public.content_library_asset_tags at
            JOIN public.content_library_tags t ON t.id = at.tag_id
            WHERE at.asset_id = %s AND t.namespace = 'emotion'
              AND at.provenance = 'human' LIMIT 1
        """, (canonical_id,))
        if curated:
            continue
        for emotion in emotions:
            execute_returning(db, """
                INSERT INTO public.content_library_tags (namespace, slug, display_name)
                VALUES ('emotion', %s, initcap(replace(%s, '-', ' ')))
                ON CONFLICT (namespace, slug) DO UPDATE SET slug = EXCLUDED.slug
                RETURNING id
            """, (emotion, emotion))
            db.execute_query("""
                INSERT INTO public.content_library_asset_tags
                    (asset_id, tag_id, provenance)
                SELECT %s, id, 'path' FROM public.content_library_tags
                WHERE namespace = 'emotion' AND slug = %s
                ON CONFLICT (asset_id, tag_id) DO NOTHING
            """, (canonical_id, emotion))
            attached += 1
    return attached


def resolve_cityids(db, bucket):
    """
    Turns path-derived slugs into canonical CIT- identifiers.

    The golden source keys cities as 'CIT-00000000002'; S3 paths carry
    'new-york'. Only a resolved match is written to cityid, and only where
    it is still blank, so a value a reviewer set is never overwritten.
    """
    rows = execute_returning(db, """
        UPDATE public.content_library_assets a
        SET cityid = c.cityid
        FROM public.cities_reference c
        WHERE a.city_slug = c.city_slug
          AND a.bucket_name = %s
          AND a.city_slug IS NOT NULL
          AND a.cityid IS NULL
        RETURNING a.id
    """, (bucket,))
    return len(rows or [])


def unresolved_city_slugs(db, bucket):
    """
    cities_reference is a lagging copy of a database this server cannot
    reach, so an unknown slug is reported rather than rejected. Rights and
    review fail closed; a replicated lookup fails soft.
    """
    rows = db.execute_query("SELECT to_regclass('public.cities_reference')")
    if not rows or rows[0][0] is None:
        return None
    rows = db.execute_query("""
        SELECT DISTINCT a.city_slug FROM public.content_library_assets a
        LEFT JOIN public.cities_reference c ON c.city_slug = a.city_slug
        WHERE a.bucket_name = %s AND a.city_slug IS NOT NULL AND c.cityid IS NULL
        ORDER BY 1
    """, (bucket,))
    return [r[0] for r in (rows or [])]


# ---------------------------------------------------------------------------
# Run bookkeeping
# ---------------------------------------------------------------------------

def open_run(db, run_id, bucket, prefix, dry_run):
    db.execute_query("""
        INSERT INTO public.content_library_inventory_runs
            (run_id, bucket_name, prefix, dry_run)
        VALUES (%s, %s, %s, %s)
    """, (run_id, bucket, prefix, dry_run))


def close_run(db, run_id, counters, complete, error=None):
    db.execute_query("""
        UPDATE public.content_library_inventory_runs SET
            completed_at = now(), listing_complete = %(complete)s,
            objects_listed = %(listed)s, markers_skipped = %(markers)s,
            exported_skipped = %(exported)s, discovered = %(discovered)s,
            unchanged = %(unchanged)s, metadata_changed = %(changed)s,
            marked_missing = %(missing)s, probed = %(probed)s,
            probe_failures = %(probe_failures)s, checksummed = %(checksummed)s,
            duplicates_found = %(duplicates)s, error_detail = %(error)s
        WHERE run_id = %(run_id)s
    """, {"run_id": run_id, "complete": complete, "error": error, **counters})


def main():
    ap = argparse.ArgumentParser(
        description="Inventory the UGC clip library into the content library.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List, classify, probe and checksum, but write nothing")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after N media objects (implies no reconciliation)")
    ap.add_argument("--no-probe", action="store_true", help="Skip ffprobe")
    ap.add_argument("--no-checksum", action="store_true", help="Skip SHA-256")
    ap.add_argument("--reprobe", action="store_true",
                    help="Re-probe objects that already have measurements")
    ap.add_argument("--allow-exported-reimport", action="store_true",
                    help="Also ingest ugc-assets/exported/ (off by default)")
    ap.add_argument("--probe-timeout", type=int, default=120)
    args = ap.parse_args()

    load_env_file()
    bucket = os.getenv("CLIPS_BUCKET", "big-city-travel-guide-clips")
    prefix = os.getenv("CLIPS_PREFIX", "ugc-assets/")
    region = os.getenv("CLIPS_REGION", "us-east-1")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set.")

    run_id = f"INV-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc)
    mode = "DRY-RUN" if args.dry_run else "LIVE"
    prefix_word = "Would be " if args.dry_run else ""

    print(f"[RUN] {run_id}  mode={mode}  s3://{bucket}/{prefix}  region={region}")

    s3 = S3Interpreter(bucket, region=region)
    counters = {"discovered": 0, "unchanged": 0, "changed": 0, "missing": 0,
                "probed": 0, "probe_failures": 0, "checksummed": 0,
                "duplicates": 0}

    objects, complete, listing = list_source_objects(
        s3, prefix, allow_exported=args.allow_exported_reimport,
        limit=args.limit or None)

    print(f"[S3 ] listed={listing['listed']}  media={len(objects)}  "
          f"markers={listing['markers']}  exported={listing['exported']}  "
          f"other={listing['non_media']}  complete={complete}")

    by_type, emotions_by_key, unrecognized = {}, {}, []
    for obj in objects:
        classified = clpaths.classify(obj["key"], prefix=prefix)
        obj["_classified"] = classified
        if not classified.recognized:
            unrecognized.append(obj["key"])
        by_type[classified.asset_type] = by_type.get(classified.asset_type, 0) + 1
        if classified.asset_type == "reaction" and classified.emotions:
            emotions_by_key[obj["key"]] = classified.emotions

    print("[CLS] " + "  ".join(f"{k or 'unrecognized'}={v}"
                               for k, v in sorted(by_type.items(), key=lambda x: str(x[0]))))

    if args.dry_run:
        for key in unrecognized:
            print(f"[WARN] unrecognized path shape: {key}")
        print(f"\n{prefix_word}registered: {len(objects)} objects")
        print(f"{prefix_word}skipped: {listing['markers']} folder markers, "
              f"{listing['exported']} exported, {listing['non_media']} non-media")
        print(f"reconciliation: {'would run' if complete else 'SKIPPED — listing incomplete'}")
        print("\nNo database changes were made.")
        return

    db = PostgresInterpreter(**parse_database_url(database_url))
    with db:
        if not db.connection:
            raise SystemExit("Could not connect to the database.")
        open_run(db, run_id, bucket, prefix, args.dry_run)

        # Per-object progress. A probe plus a full-object checksum takes
        # seconds each, so a silent loop over 73 objects is indistinguishable
        # from a hang.
        total = len(objects)
        for index, obj in enumerate(objects, start=1):
            classified = obj["_classified"]
            short_key = obj["key"][len(prefix):] if obj["key"].startswith(prefix) \
                else obj["key"]
            asset = upsert_asset(db, obj, classified, started_at)
            if asset is None:
                print(f"[{index:>3}/{total}] {short_key}  UPSERT FAILED")
                continue
            counters["discovered" if asset["inserted"] else "unchanged"] += 1
            actions = ["new" if asset["inserted"] else "seen"]

            needs_probe = args.reprobe or asset["duration_ms"] is None
            if needs_probe and not args.no_probe:
                result = probe_asset(db, s3, asset, obj["key"], args.probe_timeout)
                counters["probed"] += 1
                if result["error"]:
                    counters["probe_failures"] += 1
                    actions.append(f"PROBE FAILED ({result['error']})")
                else:
                    actions.append(
                        f"{(result['duration_ms'] or 0) / 1000:.1f}s "
                        f"{result['width']}x{result['height']} "
                        f"{result['orientation']}")

            if asset["checksum"] is None and not args.no_checksum:
                checksum_asset(db, s3, asset["id"], obj["key"])
                counters["checksummed"] += 1
                actions.append(f"sha {(obj['size'] or 0) / 1048576:.0f}MiB")

            print(f"[{index:>3}/{total}] {short_key}  {'  '.join(actions)}",
                  flush=True)

        db.execute_query(
            "UPDATE public.content_library_assets "
            "SET asset_id = 'UGC-' || lpad(id::text, 5, '0') WHERE asset_id IS NULL")

        if complete:
            counters["missing"] = reconcile_missing(db, bucket, started_at)
        else:
            print("[SKIP] listing incomplete — nothing marked missing")

        counters["duplicates"] = merge_duplicates(db, bucket)
        attached = merge_emotion_tags(db, emotions_by_key, bucket)
        resolved = resolve_cityids(db, bucket)
        unresolved = unresolved_city_slugs(db, bucket)

        close_run(db, run_id, {**counters, **{
            "listed": listing["listed"], "markers": listing["markers"],
            "exported": listing["exported"]}}, complete)

        print(f"\n[DONE] {run_id}")
        print(f"  registered   {counters['discovered']} new, {counters['unchanged']} existing")
        print(f"  probed       {counters['probed']} ({counters['probe_failures']} failed)")
        print(f"  checksummed  {counters['checksummed']}")
        print(f"  duplicates   {counters['duplicates']} marked, {attached} emotion tags merged")
        print(f"  missing      {counters['missing']}")
        print(f"  cityid       {resolved} resolved from slug")
        if unresolved is None:
            print("  cityid       cities_reference not present — check skipped")
        elif unresolved:
            print(f"  cityid       unresolved slugs: {', '.join(str(c) for c in unresolved)}")
        for key in unrecognized:
            print(f"[WARN] unrecognized path shape: {key}")


if __name__ == "__main__":
    main()
