"""
Sync the source-photo library from S3 into image_library_assets.

Lists ugc-assets/images/, skips folder markers and non-images, probes each
photo's dimensions with Pillow (photos are small), and upserts a row. Human
edits (city, place, topic, governance) are preserved with COALESCE, the same
contract the video sync uses: the folder seeds a row once, review owns it after.
Objects that have disappeared from S3 are marked missing rather than deleted.
"""
import io

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    # A missing Pillow must not break importing this module (and with it the
    # whole API). Without it, sync still runs — photos just come in without
    # probed dimensions until Pillow is installed.
    Image = None

import ImageLibraryPaths as paths

UPSERT_SQL = """
INSERT INTO public.image_library_assets (
    bucket_name, s3_key, filename, folder, size_bytes, content_type,
    width, height, orientation, city_slug, subcategory, status
) VALUES (
    %(bucket)s, %(key)s, %(filename)s, %(folder)s, %(size)s, %(content_type)s,
    %(width)s, %(height)s, %(orientation)s, %(city_slug)s, %(subcategory)s, 'pending'
)
ON CONFLICT (s3_key) DO UPDATE SET
    size_bytes   = EXCLUDED.size_bytes,
    content_type = EXCLUDED.content_type,
    filename     = EXCLUDED.filename,
    folder       = EXCLUDED.folder,
    width        = COALESCE(public.image_library_assets.width,  EXCLUDED.width),
    height       = COALESCE(public.image_library_assets.height, EXCLUDED.height),
    orientation  = COALESCE(public.image_library_assets.orientation, EXCLUDED.orientation),
    city_slug    = COALESCE(public.image_library_assets.city_slug,   EXCLUDED.city_slug),
    subcategory  = COALESCE(public.image_library_assets.subcategory, EXCLUDED.subcategory),
    missing_since = NULL,
    updated_at   = now()
RETURNING id
"""

CONTENT_TYPE = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}


def _commit_returning(db, sql, params):
    rows = db.execute_query(sql, params)
    if rows is not False and db.connection:
        db.connection.commit()
    return rows


def _known_city_slugs(db):
    """Slugs from cities_reference, so a folder segment can be recognised as a
    city regardless of whether it comes before or after the topic. Empty set if
    the (lagging) reference table isn't present."""
    try:
        rows = db.execute_query(
            "SELECT city_slug FROM public.cities_reference WHERE city_slug IS NOT NULL")
        return {r[0] for r in (rows or []) if r[0]}
    except Exception:
        return set()


def _probe_dimensions(s3, key):
    """Read the object and return (width, height, orientation) or (None,)*3."""
    if Image is None:
        return None, None, None
    try:
        buf = io.BytesIO(b"".join(s3.iter_object(key)))
        with Image.open(buf) as im:
            w, h = im.size
        orient = "portrait" if h > w else "landscape" if w > h else "square"
        return w, h, orient
    except Exception:
        return None, None, None


def sync_images(db, s3, bucket, prefix=paths.IMAGES_PREFIX, dry_run=False):
    seen_keys = []
    added = updated = skipped = 0
    known_cities = _known_city_slugs(db)

    for obj in s3.list_objects(prefix):
        key, size = obj["key"], obj.get("size", 0)
        if paths.is_folder_marker(key, size) or not paths.is_image(key):
            skipped += 1
            continue
        seen_keys.append(key)
        if dry_run:
            continue

        classified = paths.classify(key, prefix, known_city_slugs=known_cities)
        ext = "." + key.rsplit(".", 1)[-1].lower()
        width, height, orientation = _probe_dimensions(s3, key)
        params = {
            "bucket": bucket, "key": key, "filename": classified.filename,
            "folder": classified.folder, "size": size,
            "content_type": CONTENT_TYPE.get(ext, "application/octet-stream"),
            "width": width, "height": height, "orientation": orientation,
            "city_slug": classified.city_slug, "subcategory": classified.subcategory,
        }
        rows = _commit_returning(db, UPSERT_SQL, params)
        # RETURNING id always comes back; distinguishing insert vs update is not
        # worth a second round-trip, so count everything upserted as "synced".
        if rows:
            added += 1

    # Anything in the table that no longer exists in S3 is flagged missing.
    marked_missing = 0
    if not dry_run and seen_keys:
        res = db.execute_query("""
            UPDATE public.image_library_assets
               SET missing_since = COALESCE(missing_since, now()), updated_at = now()
             WHERE s3_key LIKE %(prefix)s AND s3_key <> ALL(%(seen)s)
               AND missing_since IS NULL
        """, {"prefix": prefix + "%", "seen": seen_keys})
        if res is not False and db.connection:
            db.connection.commit()
            marked_missing = getattr(res, "rowcount", 0) or 0

    return {"synced": added, "updated": updated, "skipped": skipped,
            "seen": len(seen_keys), "marked_missing": marked_missing}
