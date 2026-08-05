"""
Content library API — runs on server 3, reached only through server 2's
admin backend proxying to it.

The endpoint contracts here deliberately match the ones the admin backend
already exposes, field for field, so `admin_ui/src/ContentLibrary.tsx`
needs no change at all. The browser keeps calling the same same-origin
paths; only what sits behind them moves.

Not exposed to the internet. Bind to the private interface and let the
security group admit server 2 alone. Inbound requests must carry
X-Admin-Secret — marginally better than the admin backend, which
authenticates nothing.

Env:
    DATABASE_URL             — required; the mediamixer database
    MEDIAMIXER_ADMIN_SECRET  — required; shared with server 2's backend.
                               Deliberately distinct from server 2's own
                               ADMIN_SECRET, which authenticates it to the
                               concierge API — one secret should not open
                               two unrelated services.
    CLIPS_BUCKET             — default big-city-travel-guide-clips
    CLIPS_REGION             — default us-east-1
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import get_db  # noqa: E402
from S3Interpreter import S3Interpreter  # noqa: E402
from S3Exporter import S3Exporter  # noqa: E402
import ContentLibraryPaths as clpaths  # noqa: E402
import CaptionBuilder as captions  # noqa: E402
import ContentLibrarySelect as clselect  # noqa: E402
import ContentLibrarySync as sync  # noqa: E402
import ImageComposer as imgcomposer  # noqa: E402
import ImageLibrarySync as imgsync  # noqa: E402
import ImageLibraryPaths as imgpaths  # noqa: E402

app = FastAPI(title="MediaMixer Content Library")

_BUCKET = os.getenv("CLIPS_BUCKET", "big-city-travel-guide-clips")
_PREFIX = os.getenv("CLIPS_PREFIX", "ugc-assets/")
_REGION = os.getenv("CLIPS_REGION", "us-east-1")
_ADMIN_SECRET = os.getenv("MEDIAMIXER_ADMIN_SECRET", "")

_s3 = S3Interpreter(_BUCKET, region=_REGION)
# Write-scoped to ugc-assets/exported/ — the only place server 3's role may
# write. Poster thumbnails land under exported/thumbnails/.
_exporter = S3Exporter(_BUCKET, region=_REGION)
_THUMB_PREFIX = "ugc-assets/exported/thumbnails/"
_VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv")

# How many un-measured clips the Sync button probes+checksums inline before
# leaving the rest to the scheduled job. Kept small so the request stays
# inside the browser's timeout even when a checksum downloads a whole clip.
_SYNC_PROBE_LIMIT = int(os.getenv("SYNC_PROBE_LIMIT", "12"))
_SYNC_PROBE_TIMEOUT = int(os.getenv("SYNC_PROBE_TIMEOUT", "60"))
# Thumbnails per Sync click. Smaller than the probe batch: each spins ffmpeg
# to decode a frame (which is what lets a .mov show in the browser at all).
_SYNC_THUMB_LIMIT = int(os.getenv("SYNC_THUMB_LIMIT", "8"))
_THUMB_TIMEOUT = int(os.getenv("THUMB_TIMEOUT", "60"))


def _make_thumbnail(source_key, dest_key, timeout=_THUMB_TIMEOUT):
    """
    Extract one frame with ffmpeg and upload it as a JPEG. ffmpeg decodes the
    source server-side, so this works even for HEVC .mov the browser cannot
    play. Returns dest_key on success, None on failure.
    """
    url = _s3.presign(source_key)
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-ss", "1", "-i", url, "-frames:v", "1",
             "-vf", "scale=320:-2", "-q:v", "4", tmp],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            return None
        _exporter.put_file(tmp, dest_key, content_type="image/jpeg", overwrite=True)
        return dest_key
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

# Most renders allowed in flight at once. The worker also serializes encodes
# with a lock, so this only bounds how many can queue up behind the one that
# is running.
_MAX_INFLIGHT_RENDERS = int(os.getenv("MAX_INFLIGHT_RENDERS", "3"))

# An in-flight render older than this had its worker die before finishing
# (a crash or OOM); it is reaped so it stops counting against the guard.
# Comfortably longer than a real encode, which takes a couple of minutes.
_RENDER_STALE_MINUTES = int(os.getenv("RENDER_STALE_MINUTES", "20"))

# Columns a reviewer may write. The governance fields are here so an asset
# can be activated through the tab that already exists, rather than needing
# new UI before anything can become eligible.
_EDITABLE = {
    "asset_id", "place_name", "cityid", "country", "neighborhood", "type",
    "subtype", "category", "subcategory", "duration", "hook_compatibility",
    "notes", "status", "rights_status", "rights_source", "city_agnostic",
    "shot_type", "camera_motion", "time_of_day", "quality_score",
}

_OPTION_FIELDS = ("type", "subtype", "category", "subcategory", "country",
                  "neighborhood", "asset_type", "status", "rights_status")


def require_secret(x_admin_secret: Optional[str] = Header(None)):
    """
    Rejects anything that did not come through server 2's proxy. Not a
    substitute for the security group — it is the second layer, for the
    case where the first is misconfigured.
    """
    if not _ADMIN_SECRET:
        raise HTTPException(status_code=503,
                            detail="MEDIAMIXER_ADMIN_SECRET not configured")
    if x_admin_secret != _ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")


def _parse_hooks(value):
    """
    Normalizes a Hook Compatibility value into a list of phrases.

    Accepts a list, or a quoted comma-separated string such as
    '"Best Pizza","Hidden Gems"'. Copied in behaviour from the admin
    backend so the textarea in the existing tab keeps working unchanged.
    """
    if value is None:
        return None
    if isinstance(value, list):
        items = [str(x) for x in value]
    else:
        text = str(value).strip()
        if not text:
            return None
        quoted = re.findall(r'"([^"]*)"', text)
        items = quoted if quoted else text.split(",")
    items = [x for x in (i.strip() for i in items) if x]
    return items or None


def _quote_ident(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise HTTPException(status_code=400, detail="invalid column")
    return f'"{name}"'


def _rows_as_dicts(db, sql, params=None):
    rows = db.execute_query_as_dict(sql, params)
    return rows if isinstance(rows, list) else []


@app.get("/health")
def health(db=Depends(get_db)):
    """
    Reports 503 when the database is unreachable.

    PostgresInterpreter swallows connection errors — it prints and leaves
    the connection None — so a naive check returns ok:true over a dead
    connection and the failure only surfaces later as empty results.
    """
    if not db.connection:
        raise HTTPException(status_code=503,
                            detail="database connection failed; see journalctl")
    rows = db.execute_query("SELECT count(*) FROM public.content_library_assets")
    if not rows:
        raise HTTPException(status_code=503, detail="database query failed")
    return {"ok": True, "assets": rows[0][0]}


@app.get("/admin/content-library")
def list_content_library(hook: Optional[str] = Query(None),
                         db=Depends(get_db),
                         _=Depends(require_secret)):
    where, params = "", {}
    if hook and hook.strip():
        where = (" WHERE EXISTS (SELECT 1 FROM unnest(hook_compatibility) h "
                 "WHERE h ILIKE %(hookpat)s)")
        params["hookpat"] = f"%{hook.strip()}%"

    rows = _rows_as_dicts(
        db, f"SELECT * FROM public.content_library_assets{where} ORDER BY s3_key",
        params or None)

    tag_rows = _rows_as_dicts(db, """
        SELECT at.asset_id, t.namespace, t.slug, at.provenance
        FROM public.content_library_asset_tags at
        JOIN public.content_library_tags t ON t.id = at.tag_id
        ORDER BY t.namespace, t.slug
    """)
    tags_by_asset = {}
    for tag in tag_rows:
        tags_by_asset.setdefault(tag["asset_id"], []).append(
            {"label": f"{tag['namespace']}:{tag['slug']}",
             "provenance": tag["provenance"]})

    out = []
    for row in rows:
        row["created_at"] = str(row.get("created_at") or "")
        row["updated_at"] = str(row.get("updated_at") or "")
        row["tags"] = tags_by_asset.get(row["id"], [])
        try:
            row["preview_url"] = _s3.presign(row["s3_key"])
        except Exception:
            # A missing preview must not take the whole tab down.
            row["preview_url"] = None
        thumb = row.get("thumbnail_key")
        try:
            row["thumbnail_url"] = _s3.presign(thumb) if thumb else None
        except Exception:
            row["thumbnail_url"] = None
        out.append(row)
    return {"assets": out}


@app.get("/admin/content-library/options")
def content_library_options(field: str = Query(...),
                            db=Depends(get_db),
                            _=Depends(require_secret)):
    if field not in _OPTION_FIELDS:
        raise HTTPException(status_code=400, detail="invalid field")
    column = _quote_ident(field)
    rows = db.execute_query(
        f"SELECT DISTINCT {column} AS v FROM public.content_library_assets "
        f"WHERE {column} IS NOT NULL AND {column}::text <> '' ORDER BY 1")
    return {"values": [r[0] for r in (rows or [])]}


@app.post("/admin/content-library/sync")
def sync_content_library(db=Depends(get_db), _=Depends(require_secret)):
    """
    Registration pass only — lists S3 and upserts, without probing or
    checksumming.

    A full sync reads 658 MiB and makes 73 ffprobe calls, which is minutes
    of work and the wrong shape for an HTTP request. The scheduled
    mediamixer-sync job does the measuring; this keeps the button in the
    admin tab fast and its meaning intact ("pick up anything new").
    """
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc)
    objects, complete, listing = sync.list_source_objects(_s3, _PREFIX)
    if not complete:
        raise HTTPException(
            status_code=502,
            detail="S3 listing did not complete; nothing was changed")

    added = 0
    for obj in objects:
        classified = clpaths.classify(obj["key"], prefix=_PREFIX)
        asset = sync.upsert_asset(db, obj, classified, started_at)
        if asset and asset["inserted"]:
            added += 1

    db.execute_query(
        "UPDATE public.content_library_assets "
        "SET asset_id = 'UGC-' || lpad(id::text, 5, '0') WHERE asset_id IS NULL")

    # Probe a bounded batch of un-measured clips inline, so a clip uploaded a
    # moment ago (the end-card, say) becomes selectable straight from this
    # button instead of waiting on the scheduled mediamixer-sync job. Capped
    # because a probe plus a full-object checksum is heavy; a large backlog is
    # left to the scheduled job. Selection needs both duration_ms (probe) and
    # checksum_sha256, so both are filled here.
    probed = 0
    pending = _rows_as_dicts(db, """
        SELECT id, s3_key, status, duration_ms, checksum_sha256
        FROM public.content_library_assets
        WHERE missing_since IS NULL AND asset_type IS NOT NULL
          AND (duration_ms IS NULL OR checksum_sha256 IS NULL)
        ORDER BY id
        LIMIT %(limit)s
    """, {"limit": _SYNC_PROBE_LIMIT})
    for asset in pending:
        key = asset["s3_key"]
        try:
            if asset["duration_ms"] is None:
                sync.probe_asset(db, _s3, asset, key, _SYNC_PROBE_TIMEOUT)
            if asset["checksum_sha256"] is None:
                sync.checksum_asset(db, _s3, asset["id"], key)
            probed += 1
        except Exception:
            continue

    # Poster thumbnails for video clips that lack one — a real frame the grid
    # can show even for footage the browser cannot decode. Bounded per click.
    thumbs = 0
    pending_thumbs = _rows_as_dicts(db, """
        SELECT id, s3_key FROM public.content_library_assets
        WHERE thumbnail_key IS NULL AND missing_since IS NULL
          AND duration_ms IS NOT NULL
          AND lower(s3_key) ~ '\\.(mp4|mov|m4v|webm|avi|mkv)$'
        ORDER BY id LIMIT %(limit)s
    """, {"limit": _SYNC_THUMB_LIMIT})
    for asset in pending_thumbs:
        dest = f"{_THUMB_PREFIX}{asset['id']}.jpg"
        try:
            if _make_thumbnail(asset["s3_key"], dest):
                db.execute_query(
                    "UPDATE public.content_library_assets SET thumbnail_key = %s "
                    "WHERE id = %s", (dest, asset["id"]))
                thumbs += 1
        except Exception:
            continue

    # 'durations' keeps the admin tab's handler unchanged.
    return {"added": added, "durations": probed, "thumbs": thumbs,
            "listed": len(objects)}


# Namespaces a reviewer may write. `emotion` is included so a wrong
# folder-derived tag can be corrected; the others come from the controlled
# vocabularies in 04-asset-standards.md.
_TAG_NAMESPACES = ("emotion", "theme", "mood", "visual", "audience", "compatibility")


def parse_tags(value):
    """
    Accepts "emotion:surprised, theme:hidden-gem" or a list of the same.

    A bare slug is read as `theme`, which is where most descriptive tags
    belong. Unknown namespaces are rejected rather than silently created,
    since a typo would otherwise become a namespace nobody ever queries.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    parsed = []
    for item in items:
        text = str(item).strip().lower()
        if not text:
            continue
        namespace, _, slug = text.partition(":")
        if not slug:
            namespace, slug = "theme", namespace
        slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
        if not slug:
            continue
        if namespace not in _TAG_NAMESPACES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown tag namespace {namespace!r}; "
                       f"use one of {', '.join(_TAG_NAMESPACES)}")
        if (namespace, slug) not in parsed:
            parsed.append((namespace, slug))
    return parsed


def replace_tags(db, asset_pk, tags):
    """
    Replaces an asset's tags wholesale, marking them provenance 'human'.

    Wholesale rather than additive because removal has to be expressible —
    the reason for having this at all is correcting a wrong folder-derived
    emotion. The 'human' provenance is what stops the next sync putting it
    straight back.
    """
    db.execute_query(
        "DELETE FROM public.content_library_asset_tags WHERE asset_id = %s",
        (asset_pk,))
    for namespace, slug in tags:
        db.execute_query("""
            INSERT INTO public.content_library_tags (namespace, slug, display_name)
            VALUES (%s, %s, initcap(replace(%s, '-', ' ')))
            ON CONFLICT (namespace, slug) DO NOTHING
        """, (namespace, slug, slug))
        db.execute_query("""
            INSERT INTO public.content_library_asset_tags
                (asset_id, tag_id, provenance, reviewed_at)
            SELECT %s, id, 'human', now() FROM public.content_library_tags
            WHERE namespace = %s AND slug = %s
            ON CONFLICT (asset_id, tag_id) DO UPDATE SET provenance = 'human'
        """, (asset_pk, namespace, slug))


@app.get("/admin/tags")
def list_tags(db=Depends(get_db), _=Depends(require_secret)):
    """Every tag in use, for autocomplete."""
    rows = _rows_as_dicts(db, """
        SELECT t.namespace, t.slug, count(at.asset_id) AS assets
        FROM public.content_library_tags t
        LEFT JOIN public.content_library_asset_tags at ON at.tag_id = t.id
        GROUP BY t.namespace, t.slug ORDER BY t.namespace, t.slug
    """)
    return {"tags": [{"label": f"{r['namespace']}:{r['slug']}",
                      "assets": r["assets"]} for r in rows],
            "namespaces": list(_TAG_NAMESPACES)}


class ContentLibraryUpdate(BaseModel):
    values: Dict[str, Any]


@app.put("/admin/content-library/{asset_pk}")
def update_content_library(asset_pk: int, body: ContentLibraryUpdate,
                           db=Depends(get_db), _=Depends(require_secret)):
    if "tags" in body.values:
        replace_tags(db, asset_pk, parse_tags(body.values["tags"]))

    values = {k: v for k, v in body.values.items() if k in _EDITABLE}
    if not values:
        return {"ok": True}
    if "hook_compatibility" in values:
        values["hook_compatibility"] = _parse_hooks(values["hook_compatibility"])
    if "quality_score" in values:
        # A SMALLINT column; the grid sends a string. Blank clears it, and a
        # value is clamped to 1..5 so a stray entry can't poison scoring.
        raw = _to_number(values["quality_score"], int)
        values["quality_score"] = None if raw is None else max(1, min(5, raw))

    assignments = ", ".join(f"{_quote_ident(k)} = %({k})s" for k in values)
    result = db.execute_query(
        f"UPDATE public.content_library_assets SET {assignments} WHERE id = %(id)s",
        {**values, "id": asset_pk})
    if result is False:
        raise HTTPException(status_code=409,
                            detail="update rejected (asset_id must be unique)")
    return {"ok": True}


@app.delete("/admin/content-library/{asset_pk}")
def delete_content_library(asset_pk: int, db=Depends(get_db),
                           _=Depends(require_secret)):
    """
    Removes the catalog row. The S3 object is untouched — this service
    cannot delete from S3 even if asked, because S3Interpreter has no
    delete method and the instance role denies the action outright.
    """
    result = db.execute_query(
        "DELETE FROM public.content_library_assets WHERE id = %s", (asset_pk,))
    if result is False:
        raise HTTPException(
            status_code=409,
            detail="cannot delete: asset is referenced by a render")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Music library
#
# Same shape as the content library above, over content_library_music_tracks.
# The reason a music track is its own kind and not asset_type='music' is the
# licence: a bed carries an attribution string and the two questions that
# actually gate use — is commercial use allowed, are derivatives allowed —
# which a clip never does. These promote a paid product, so both matter.
# ---------------------------------------------------------------------------
_MUSIC_PREFIX = os.getenv("MUSIC_PREFIX", "ugc-assets/music/")
_AUDIO_EXT = (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus")

# Columns a reviewer may write. Object and probe facts are omitted: those are
# established by sync and ffprobe, never typed.
_MUSIC_EDITABLE = {
    "track_id", "title", "artist", "album", "genre", "mood", "bpm", "energy",
    "instrumental", "tags", "license", "license_url", "source", "source_url",
    "commercial_use_allowed", "derivatives_allowed", "attribution_required",
    "attribution_text", "license_expires_at", "license_proof_s3_key", "notes",
    "status", "reviewed_by", "reviewed_at",
}
_MUSIC_OPTION_FIELDS = ("genre", "mood", "license", "source", "status")
_MUSIC_BOOL = {"commercial_use_allowed", "derivatives_allowed",
               "attribution_required", "instrumental"}


def _to_bool(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t")


def _to_number(v, cast):
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def _parse_str_list(value):
    """Freeform tags: a list, or a comma-separated string. Empty -> []."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    seen, out = set(), []
    for item in items:
        s = str(item).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def _coerce_music(values):
    out = {}
    for k, v in values.items():
        if k not in _MUSIC_EDITABLE:
            continue
        if k == "tags":
            out[k] = _parse_str_list(v)
        elif k in _MUSIC_BOOL:
            out[k] = _to_bool(v)
        elif k == "bpm":
            out[k] = _to_number(v, float)
        elif k == "energy":
            out[k] = _to_number(v, int)
        else:
            out[k] = v if v != "" else None
    return out


_MUSIC_PROBE_DEFAULT = {
    "duration_ms": None, "sample_rate": None, "channels": None,
    "audio_codec": None, "title": None, "artist": None, "album": None,
    "genre": None, "mood": None, "bpm": None,
}


def _tag(tags, *names):
    """Case-insensitive first-non-empty lookup across candidate tag names."""
    low = {str(k).lower(): v for k, v in (tags or {}).items()}
    for name in names:
        value = low.get(name.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _title_from_key(key):
    """
    A human title from the object key, to reconcile against YouTube.

    'ugc-assets/music/Ancient%20History%20-%20Bosley.mp3' -> 'Ancient History
    - Bosley'. Percent-decoded, anything after a '?' dropped, and the audio
    extension removed.
    """
    name = key.rsplit("/", 1)[-1].split("?", 1)[0]
    name = unquote(name)
    low = name.lower()
    for ext in _AUDIO_EXT:
        if low.endswith(ext):
            name = name[: -len(ext)]
            break
    return name.strip() or None


def _probe_audio(url, timeout=60):
    """
    Facts and embedded metadata, read over the presigned URL.

    Beyond duration/sample-rate, ffprobe surfaces the file's ID3 tags:
    title/artist/album/genre/BPM are lifted straight from them when present,
    so a well-tagged download needs little hand entry. Mood is read only if
    the file actually carries one — most do not — and is left for a reviewer
    otherwise.
    """
    proc = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", url],
        capture_output=True, text=True, timeout=timeout)
    data = json.loads(proc.stdout or "{}")
    fmt = data.get("format", {})
    astream = next((s for s in data.get("streams", [])
                    if s.get("codec_type") == "audio"), {})
    dur = fmt.get("duration") or astream.get("duration")
    tags = {**(fmt.get("tags") or {}), **(astream.get("tags") or {})}
    return {
        "duration_ms": int(float(dur) * 1000) if dur else None,
        "sample_rate": _to_number(astream.get("sample_rate"), int),
        "channels": astream.get("channels"),
        "audio_codec": astream.get("codec_name"),
        "title": _tag(tags, "title"),
        "artist": _tag(tags, "artist", "album_artist", "performer"),
        "album": _tag(tags, "album"),
        "genre": _tag(tags, "genre"),
        "mood": _tag(tags, "mood", "TMOO"),
        "bpm": _to_number(_tag(tags, "TBPM", "bpm", "tempo"), float),
    }


_MUSIC_INSERT_SQL = """
INSERT INTO public.content_library_music_tracks
    (bucket_name, s3_key, filename, folder, size_bytes, content_type, etag,
     s3_last_modified_at, duration_ms, sample_rate, channels, audio_codec,
     title, artist, album, genre, mood, bpm)
VALUES
    (%(bucket)s, %(key)s, %(filename)s, %(folder)s, %(size)s, %(content_type)s,
     %(etag)s, %(last_modified)s, %(duration_ms)s, %(sample_rate)s,
     %(channels)s, %(audio_codec)s,
     %(title)s, %(artist)s, %(album)s, %(genre)s, %(mood)s, %(bpm)s)
ON CONFLICT (s3_key) DO NOTHING
"""


@app.get("/admin/music-library")
def list_music_library(db=Depends(get_db), _=Depends(require_secret)):
    rows = _rows_as_dicts(
        db, "SELECT * FROM public.content_library_music_tracks ORDER BY s3_key")
    out = []
    for row in rows:
        row["created_at"] = str(row.get("created_at") or "")
        row["updated_at"] = str(row.get("updated_at") or "")
        row["license_expires_at"] = str(row.get("license_expires_at") or "")
        try:
            row["preview_url"] = _s3.presign(row["s3_key"])
        except Exception:
            row["preview_url"] = None
        out.append(row)
    return {"tracks": out}


@app.get("/admin/music-library/options")
def music_library_options(field: str = Query(...), db=Depends(get_db),
                          _=Depends(require_secret)):
    if field not in _MUSIC_OPTION_FIELDS:
        raise HTTPException(status_code=400, detail="invalid field")
    column = _quote_ident(field)
    rows = db.execute_query(
        f"SELECT DISTINCT {column} AS v FROM public.content_library_music_tracks "
        f"WHERE {column} IS NOT NULL AND {column}::text <> '' ORDER BY 1")
    return {"values": [r[0] for r in (rows or [])]}


@app.post("/admin/music-library/sync")
def sync_music_library(db=Depends(get_db), _=Depends(require_secret)):
    """
    Registers new tracks under the music prefix and probes each once.

    Unlike the clip sync, this probes inline: the music library is small and
    an audio ffprobe reads only the header, so a duration is available the
    moment a track appears rather than waiting on a scheduled pass.
    """
    existing = {}
    for r in (db.execute_query(
            "SELECT s3_key, id, title "
            "FROM public.content_library_music_tracks") or []):
        existing[r[0]] = {"id": r[1], "title": r[2]}
    added, probed, retitled, listed = 0, 0, 0, 0
    try:
        for obj in _s3.list_objects(_MUSIC_PREFIX):
            key, size = obj["key"], obj["size"]
            if key.endswith("/") or size == 0:
                continue
            if not key.lower().endswith(_AUDIO_EXT):
                continue
            listed += 1
            row = existing.get(key)
            title = _title_from_key(key)

            if row is not None:
                # Fill the title from the filename for an already-synced track
                # that has none. Cheap (no probe) and never overwrites an edit.
                if title and not row["title"]:
                    db.execute_query(
                        "UPDATE public.content_library_music_tracks SET title = %s "
                        "WHERE id = %s AND title IS NULL", (title, row["id"]))
                    retitled += 1
                continue

            meta = dict(_MUSIC_PROBE_DEFAULT)
            content_type = None
            try:
                content_type = _s3.head(key).get("content_type")
            except Exception:
                pass
            try:
                meta = _probe_audio(_s3.presign(key))
                if meta.get("duration_ms"):
                    probed += 1
            except Exception:
                pass
            # The decoded filename is the title, so it lines up with YouTube.
            meta["title"] = title or meta.get("title")
            db.execute_query(_MUSIC_INSERT_SQL, {
                "bucket": _BUCKET, "key": key,
                "filename": key.rsplit("/", 1)[-1],
                "folder": (key.rsplit("/", 1)[0] + "/") if "/" in key else "",
                "size": size, "content_type": content_type,
                "etag": obj.get("etag"), "last_modified": obj.get("last_modified"),
                **meta})
            added += 1
        db.execute_query(
            "UPDATE public.content_library_music_tracks "
            "SET track_id = 'MUS-' || lpad(id::text, 5, '0') WHERE track_id IS NULL")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"S3 sync error: {str(exc)[:300]}")
    return {"added": added, "probed": probed, "retitled": retitled, "listed": listed}


@app.put("/admin/music-library/{track_pk}")
def update_music_library(track_pk: int, body: ContentLibraryUpdate,
                         db=Depends(get_db), _=Depends(require_secret)):
    values = _coerce_music(body.values)
    if not values:
        return {"ok": True}

    current = _rows_as_dicts(
        db, "SELECT status, attribution_required, attribution_text "
            "FROM public.content_library_music_tracks WHERE id = %s", (track_pk,))
    if not current:
        raise HTTPException(status_code=404, detail="track not found")
    merged = {**current[0], **values}
    text_val = merged.get("attribution_text") or ""
    if (str(merged.get("status")) == "active"
            and merged.get("attribution_required")
            and not str(text_val).strip()):
        # The DB CHECK enforces this too; caught here for a legible message.
        raise HTTPException(
            status_code=422,
            detail="Cannot activate: this track's licence requires attribution "
                   "but Attribution Text is empty.")

    assignments = ", ".join(f"{_quote_ident(k)} = %({k})s" for k in values)
    params = {**values, "id": track_pk}
    result = db.execute_query(
        f"UPDATE public.content_library_music_tracks SET {assignments} "
        f"WHERE id = %(id)s", params)
    if result is False:
        raise HTTPException(status_code=409,
                            detail="update rejected (track_id must be unique)")
    return {"ok": True}


@app.delete("/admin/music-library/{track_pk}")
def delete_music_library(track_pk: int, db=Depends(get_db),
                         _=Depends(require_secret)):
    result = db.execute_query(
        "DELETE FROM public.content_library_music_tracks WHERE id = %s", (track_pk,))
    if result is False:
        raise HTTPException(
            status_code=409,
            detail="cannot delete: track is credited by a render")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Video briefs and renders
#
# A brief is what a person authors. A recipe is generated from it and is
# immutable — there is deliberately no endpoint to edit one, because a
# hand-edited recipe would no longer explain the video it produced.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE_DIR = _REPO_ROOT / "templates"
_PYTHON = os.getenv("MEDIAMIXER_PYTHON", sys.executable)


class BriefBody(BaseModel):
    brief: Dict[str, Any]


def _brief_from(payload: Dict[str, Any]) -> clselect.VideoBrief:
    cleaned = {k: v for k, v in payload.items() if v not in ("", None)}
    return clselect.VideoBrief.from_dict(cleaned)


@app.get("/admin/templates")
def list_templates(_=Depends(require_secret)):
    """
    The shapes available to a brief, with enough detail for the UI to draw
    each one as a timeline.
    """
    out = []
    for path in sorted(_TEMPLATE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({
            "template_id": data["template_id"],
            "version": data["version"],
            "description": data.get("description", ""),
            "has_reaction_slot": any(
                "reaction" in s.get("asset_types", []) for s in data["slots"]),
            "slots": [{
                "role": s["role"],
                "asset_types": s["asset_types"],
                "preferred_ms": s["preferred_ms"],
                "min_ms": s["min_ms"],
                "max_ms": s["max_ms"],
                "required": s.get("required", True),
                "prefer_subtypes": s.get("prefer_subtypes", []),
                "require_shot_type": s.get("require_shot_type"),
                "require_topic_match": s.get("require_topic_match", False),
                "match_mood": s.get("match_mood", False),
                "notes": s.get("notes", ""),
            } for s in data["slots"]],
            "captions": data.get("captions", []),
        })
    return {"templates": out}


@app.get("/admin/render-cities")
def render_cities(db=Depends(get_db), _=Depends(require_secret)):
    """
    Cities that actually have active footage, so the brief form offers only
    what can produce a video rather than all 892.
    """
    rows = _rows_as_dicts(db, """
        SELECT a.cityid, COALESCE(c.cityname, a.cityid) AS cityname,
               count(*) FILTER (WHERE a.asset_type = 'broll') AS broll,
               count(*) FILTER (WHERE a.asset_type = 'app') AS app
        FROM public.content_library_assets a
        LEFT JOIN public.cities_reference c ON c.cityid = a.cityid
        WHERE a.status = 'active'
          AND a.rights_status IN ('owned', 'licensed')
          AND a.duplicate_of_asset_id IS NULL
          AND a.cityid IS NOT NULL
        GROUP BY a.cityid, c.cityname
        ORDER BY 2
    """)
    return {"cities": rows}


@app.get("/admin/render-topics")
def render_topics(cityid: Optional[str] = Query(None), db=Depends(get_db),
                  _=Depends(require_secret)):
    """Subcategories with active footage, optionally scoped to one city."""
    sql = """
        SELECT lower(subcategory) AS topic, count(*) AS clips
        FROM public.content_library_assets
        WHERE status = 'active' AND rights_status IN ('owned', 'licensed')
          AND duplicate_of_asset_id IS NULL
          AND asset_type = 'broll' AND subcategory IS NOT NULL
    """
    params = {}
    if cityid:
        sql += " AND cityid = %(cityid)s"
        params["cityid"] = cityid
    sql += " GROUP BY 1 ORDER BY 2 DESC, 1"
    return {"topics": _rows_as_dicts(db, sql, params or None)}


@app.post("/admin/renders/preview")
def preview_render(body: BriefBody, db=Depends(get_db), _=Depends(require_secret)):
    """
    Runs selection and returns the recipe without rendering anything.

    A brief that cannot be filled returns 422 with the diagnostics rather
    than an empty result, because "which slot could not be filled and what
    was it looking for" is the useful answer.
    """
    try:
        recipe = clselect.select(db, _brief_from(body.brief))
    except clselect.SelectionError as failure:
        raise HTTPException(status_code=422, detail=failure.as_dict())

    brief = _brief_from(body.brief)
    plan = captions.plan_for_recipe(db, recipe, brief.caption_overrides)
    recipe["captions"] = [c.as_dict() for c in plan.captions]

    errors = vr_validate(recipe)
    return {"recipe": recipe, "validation_errors": errors,
            "total_duration_ms": recipe["total_duration_ms"],
            "skipped_optional_slots": recipe.get("skipped_optional_slots", []),
            # Which slots carry text, so the UI can offer an override box
            # for each one without knowing the template.
            "caption_slots": [
                {"role": spec["role"], "style": spec.get("style", "label"),
                 "pattern": spec["pattern"]}
                for spec in (recipe.get("caption_specs") or [])],
            "caption_problems": plan.unresolved}


def vr_validate(recipe):
    import VideoRenderer as vr
    return vr.validate_recipe(recipe)


def _reap_and_guard(db):
    """
    Reap renders orphaned by a dead worker, then refuse if too many are still
    in flight. Shared by the fresh-brief and edited-recipe render paths.
    """
    db.execute_query(
        "UPDATE public.content_library_renders "
        "SET state='failed', error_code='orphaned', "
        "    error_detail='worker did not finish (likely terminated); reaped', "
        "    completed_at=now(), updated_at=now() "
        "WHERE state IN ('planned','queued','rendering','validating') "
        "  AND created_at < now() - make_interval(mins => %(mins)s)",
        {"mins": _RENDER_STALE_MINUTES})
    inflight = db.execute_query(
        "SELECT count(*) FROM public.content_library_renders "
        "WHERE state IN ('planned','queued','rendering','validating')")
    running = inflight[0][0] if inflight else 0
    if running >= _MAX_INFLIGHT_RENDERS:
        raise HTTPException(
            status_code=429,
            detail=f"{running} render(s) already in progress. Encodes run one "
                   f"at a time and are memory-heavy — wait for them to finish "
                   f"before starting more.")


def _require_scratch():
    scratch = os.getenv("SCRATCH_DIR", "/opt/mediamixer/scratch")
    if not os.path.isdir(scratch) or not os.access(scratch, os.W_OK):
        raise HTTPException(
            status_code=503,
            detail=f"scratch directory {scratch} is not writable by this "
                   f"service. If it exists and is owned correctly, the unit "
                   f"needs ReadWritePaths={scratch} — systemd's sandbox is "
                   f"inherited by the render worker.")
    return scratch


def _spawn_worker(args):
    try:
        # stdio is inherited so the worker's output lands in this service's
        # journal. Discarding it makes an early crash completely silent.
        subprocess.Popen(args, cwd=str(_REPO_ROOT), start_new_session=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not start render: {exc}")


@app.post("/admin/renders")
def start_render(body: BriefBody, db=Depends(get_db), _=Depends(require_secret)):
    """
    Starts a render in the background and returns immediately.

    An encode takes minutes, which is far longer than an HTTP request should
    hold. Selection runs synchronously first so an unfillable brief fails
    here with a useful message instead of appearing to start and then dying
    in a log the operator never sees.
    """
    _reap_and_guard(db)
    brief = _brief_from(body.brief)
    try:
        clselect.select(db, brief)
    except clselect.SelectionError as failure:
        raise HTTPException(status_code=422, detail=failure.as_dict())

    _require_scratch()
    environment = body.brief.get("environment", "dev")
    args = [_PYTHON, str(_REPO_ROOT / "RenderWorker.py"),
            "--brief", json.dumps(body.brief), "--environment", environment]
    _spawn_worker(args)

    # The worker creates its own render row within a second or two; the UI
    # picks it up by polling rather than being told an id that does not
    # exist yet.
    return {"started": True, "environment": environment}


class RecipeBody(BaseModel):
    recipe: Dict[str, Any]


@app.post("/admin/renders/edited")
def render_edited(body: RecipeBody, db=Depends(get_db), _=Depends(require_secret)):
    """
    Re-render an edited recipe as a *new* video — the operator changed a
    caption, swapped a clip, or swapped the bed in the tab. Recipes stay
    immutable per render; this renders a supplied copy rather than mutating
    the original, so every video is still explained by its own recipe.
    """
    _reap_and_guard(db)
    recipe = body.recipe or {}
    errors = vr_validate(recipe)
    if errors:
        raise HTTPException(status_code=422,
                            detail={"error": "recipe_invalid", "detail": errors})

    scratch = _require_scratch()
    environment = (recipe.get("brief") or {}).get("environment", "dev")
    fd, path = tempfile.mkstemp(prefix="editrecipe_", suffix=".json", dir=scratch)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(recipe, handle)
    args = [_PYTHON, str(_REPO_ROOT / "RenderWorker.py"),
            "--recipe-file", path, "--environment", environment]
    _spawn_worker(args)
    return {"started": True, "environment": environment, "edited": True}


@app.get("/admin/renders/{render_id}/alternatives")
def render_alternatives(render_id: str, sequence_no: int = Query(...),
                        db=Depends(get_db), _=Depends(require_secret)):
    """
    Eligible swap candidates for one timeline slot of a render, so the editor
    can replace a single clip. Same-kind, city-eligible, active, probed,
    portrait, and at least as long as the slot needs.
    """
    rows = _rows_as_dicts(
        db, "SELECT recipe FROM public.content_library_renders WHERE render_id = %s",
        (render_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="no such render")
    recipe = rows[0]["recipe"]
    if isinstance(recipe, str):
        recipe = json.loads(recipe)
    timeline = (recipe or {}).get("timeline") or []
    if sequence_no < 0 or sequence_no >= len(timeline):
        raise HTTPException(status_code=400, detail="sequence_no out of range")
    clip = timeline[sequence_no]
    take_ms = int(clip["source_out_ms"]) - int(clip["source_in_ms"])

    cur = _rows_as_dicts(
        db, "SELECT asset_type FROM public.content_library_assets WHERE id = %s",
        (clip.get("asset_pk"),))
    asset_type = cur[0]["asset_type"] if cur else None
    if not asset_type:
        return {"role": clip.get("role"), "take_ms": take_ms, "alternatives": []}

    # Reactions are a shared, city-agnostic library — a performance is not tied
    # to a place, a topic, or a length. For those, offer the whole library so an
    # editor can pick any reaction, not only ones long enough for the slot or
    # matching the brief. The client re-flows the timeline when a shorter clip
    # is chosen. Other asset types stay scoped and keep the duration floor,
    # since a shorter b-roll/app clip would leave the slot's segment short.
    unrestricted = asset_type == "reaction"
    brief = clselect.VideoBrief.from_dict(recipe.get("brief") or {})
    slot = clselect.Slot(role=clip.get("role", "x"), asset_types=[asset_type],
                         min_ms=0 if unrestricted else take_ms,
                         preferred_ms=take_ms, max_ms=take_ms,
                         city_agnostic_ok=(asset_type in ("cta", "reaction")))
    try:
        candidates = clselect.eligible_candidates(db, brief, slot)
    except Exception:
        candidates = []

    out = []
    for cand in (candidates or []):
        if cand.get("id") == clip.get("asset_pk"):
            continue
        if not unrestricted and (cand.get("duration_ms") or 0) < take_ms:
            continue
        try:
            preview = _s3.presign(cand["s3_key"])
        except Exception:
            preview = None
        out.append({
            "id": cand.get("id"), "asset_id": cand.get("asset_id"),
            "s3_key": cand.get("s3_key"), "s3_version_id": cand.get("s3_version_id"),
            "checksum_sha256": cand.get("checksum_sha256"),
            "duration_ms": cand.get("duration_ms"),
            "place_name": cand.get("place_name"), "subcategory": cand.get("subcategory"),
            "notes": cand.get("notes"),
            "preview_url": preview,
        })
    return {"role": clip.get("role"), "take_ms": take_ms,
            "unrestricted": unrestricted, "alternatives": out}


@app.get("/admin/renders")
def list_renders(limit: int = Query(25, ge=1, le=200), db=Depends(get_db),
                 _=Depends(require_secret)):
    rows = _rows_as_dicts(db, """
        SELECT r.id, r.render_id, r.state, r.environment, r.cityid, r.topic,
               r.template_id, r.target_duration_ms, r.actual_duration_ms,
               r.brief->>'neighborhoods' AS neighborhoods,
               r.brief->>'mood' AS mood,
               r.brief->>'feature' AS feature,
               r.error_code, r.error_detail, r.created_at, r.completed_at,
               c.cityname,
               (SELECT count(*) FROM public.content_library_render_artifacts a
                 WHERE a.render_id = r.id) AS artifacts
        FROM public.content_library_renders r
        LEFT JOIN public.cities_reference c ON c.cityid = r.cityid
        ORDER BY r.id DESC LIMIT %(limit)s
    """, {"limit": limit})
    for row in rows:
        row["created_at"] = str(row.get("created_at") or "")
        row["completed_at"] = str(row.get("completed_at") or "")
    return {"renders": rows}


@app.get("/admin/renders/{render_id}")
def render_detail(render_id: str, db=Depends(get_db), _=Depends(require_secret)):
    rows = _rows_as_dicts(db, """
        SELECT * FROM public.content_library_renders WHERE render_id = %s
    """, (render_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="no such render")
    render = rows[0]
    render["created_at"] = str(render.get("created_at") or "")
    render["completed_at"] = str(render.get("completed_at") or "")

    # A fresh render stores caption_specs (patterns), not resolved text — the
    # worker resolves them at render time and does not persist them back. Resolve
    # them here with the same builder and overrides so the editor can pre-fill
    # each clip's text box with what was actually burned in, instead of the
    # operator re-typing it. Edited renders already carry literal captions
    # (captions_frozen); leave those exactly as authored.
    recipe = render.get("recipe")
    if isinstance(recipe, str):
        try:
            recipe = json.loads(recipe)
        except Exception:
            recipe = None
    if isinstance(recipe, dict):
        if not recipe.get("captions") and not recipe.get("captions_frozen"):
            try:
                overrides = (recipe.get("brief") or {}).get("caption_overrides") or None
                plan = captions.plan_for_recipe(db, recipe, overrides=overrides)
                recipe["captions"] = [c.as_dict() for c in plan.captions]
            except Exception:
                pass
        render["recipe"] = recipe

    artifacts = _rows_as_dicts(db, """
        SELECT role, s3_key, size_bytes, content_type, checksum_sha256
        FROM public.content_library_render_artifacts
        WHERE render_id = %s ORDER BY role
    """, (render["id"],))
    for artifact in artifacts:
        try:
            artifact["url"] = _s3.presign(artifact["s3_key"])
        except Exception:
            artifact["url"] = None

    clips = _rows_as_dicts(db, """
        SELECT ra.sequence_no, ra.role, ra.source_in_ms, ra.source_out_ms,
               ra.timeline_in_ms, a.asset_id, a.place_name, a.subtype,
               a.subcategory, a.s3_key
        FROM public.content_library_render_assets ra
        JOIN public.content_library_assets a ON a.id = ra.asset_id
        WHERE ra.render_id = %s ORDER BY ra.sequence_no
    """, (render["id"],))

    return {"render": render, "artifacts": artifacts, "clips": clips}


# ===========================================================================
# Images — a still-image counterpart to the video tools.
#
# Source photos live under ugc-assets/images/ (you upload them; the video sync
# skips that tree). Composed outputs land under ugc-assets/exported/images/ —
# server 3's only writable prefix. Composition is Pillow, sub-second, so the
# compose endpoint is synchronous: no worker, no queue.
# ===========================================================================
_IMAGES_SRC_PREFIX = os.getenv("IMAGES_PREFIX", "ugc-assets/images/")
_IMAGES_OUT_PREFIX = "ugc-assets/exported/images/"
_IMAGE_TEMPLATE_DIR = _REPO_ROOT / "image_templates"
_IMAGE_EDITABLE = {
    "place_name", "city_slug", "cityid", "country", "category", "subcategory",
    "type", "subtype", "neighborhood", "hook_compatibility", "tags",
    "notes", "status", "rights_status", "quality_score",
}
_IMAGE_OPTION_FIELDS = ("city_slug", "country", "subcategory", "category",
                        "type", "subtype", "neighborhood", "orientation",
                        "status", "rights_status")
# TEXT[] columns the grid sends as a comma/quoted string, parsed like the
# video library's hook field.
_IMAGE_ARRAY_FIELDS = ("hook_compatibility", "tags")


def _download_pil(key):
    """Fetch an S3 object into a Pillow image (photos are small)."""
    from PIL import Image
    buf = io.BytesIO(b"".join(_s3.iter_object(key)))
    return Image.open(buf)


@app.get("/admin/image-templates")
def list_image_templates(_=Depends(require_secret)):
    """Every image layout, with slot geometry so the UI can draw and fill it."""
    out = []
    for path in sorted(_IMAGE_TEMPLATE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({
            "template_id": data["template_id"],
            "version": data.get("version", 1),
            "description": data.get("description", ""),
            "canvas": data.get("canvas", {}),
            "image_slots": data.get("image_slots", []),
            "text_slots": data.get("text_slots", []),
        })
    return {"templates": out}


@app.get("/admin/image-library")
def list_image_library(db=Depends(get_db), _=Depends(require_secret)):
    rows = _rows_as_dicts(
        db, "SELECT * FROM public.image_library_assets ORDER BY s3_key")
    out = []
    for row in rows:
        row["created_at"] = str(row.get("created_at") or "")
        row["updated_at"] = str(row.get("updated_at") or "")
        try:
            row["preview_url"] = _s3.presign(row["s3_key"])
        except Exception:
            row["preview_url"] = None
        out.append(row)
    return {"assets": out}


@app.get("/admin/image-library/options")
def image_library_options(field: str = Query(...), db=Depends(get_db),
                          _=Depends(require_secret)):
    if field not in _IMAGE_OPTION_FIELDS:
        raise HTTPException(status_code=400, detail="invalid field")
    column = _quote_ident(field)
    rows = db.execute_query(
        f"SELECT DISTINCT {column} AS v FROM public.image_library_assets "
        f"WHERE {column} IS NOT NULL AND {column}::text <> '' ORDER BY 1")
    return {"values": [r[0] for r in (rows or [])]}


@app.post("/admin/image-library/sync")
def sync_image_library(db=Depends(get_db), _=Depends(require_secret)):
    """List ugc-assets/images/ and upsert into the photo library (probes size)."""
    summary = imgsync.sync_images(db, _s3, _BUCKET, _IMAGES_SRC_PREFIX)
    db.execute_query(
        "UPDATE public.image_library_assets "
        "SET asset_id = 'IMG-' || lpad(id::text, 5, '0') WHERE asset_id IS NULL")
    return {"ok": True, **summary}


class ImageLibraryUpdate(BaseModel):
    values: Dict[str, Any] = {}


@app.put("/admin/image-library/{asset_pk}")
def update_image_library(asset_pk: int, body: ImageLibraryUpdate,
                         db=Depends(get_db), _=Depends(require_secret)):
    values = {k: v for k, v in body.values.items() if k in _IMAGE_EDITABLE}
    if not values:
        return {"ok": True}
    if "quality_score" in values:
        raw = _to_number(values["quality_score"], int)
        values["quality_score"] = None if raw is None else max(1, min(5, raw))
    for field in _IMAGE_ARRAY_FIELDS:
        if field in values:
            values[field] = _parse_hooks(values[field])
    assignments = ", ".join(f"{_quote_ident(k)} = %({k})s" for k in values)
    result = db.execute_query(
        f"UPDATE public.image_library_assets SET {assignments}, updated_at = now() "
        f"WHERE id = %(id)s", {**values, "id": asset_pk})
    if result is False:
        raise HTTPException(status_code=409, detail="update rejected")
    return {"ok": True}


@app.delete("/admin/image-library/{asset_pk}")
def delete_image_library(asset_pk: int, db=Depends(get_db),
                         _=Depends(require_secret)):
    """Removes the catalog row; the S3 object is untouched (no delete access)."""
    result = db.execute_query(
        "DELETE FROM public.image_library_assets WHERE id = %s", (asset_pk,))
    if result is False:
        raise HTTPException(status_code=409, detail="delete rejected")
    return {"ok": True}


class ImageComposeBody(BaseModel):
    template_id: str
    slots: Dict[str, int] = {}     # image slot name -> image_library_assets.id
    texts: Dict[str, str] = {}     # text slot name -> literal text
    cityid: Optional[str] = None
    topic: Optional[str] = None


@app.post("/admin/images/compose")
def compose_image(body: ImageComposeBody, db=Depends(get_db),
                  _=Depends(require_secret)):
    """
    Compose one still from a template + chosen photos + text, upload it to
    ugc-assets/exported/images/, and record it. Synchronous — Pillow is fast.
    """
    try:
        template = imgcomposer.load_image_template(body.template_id,
                                                   _IMAGE_TEMPLATE_DIR)
    except imgcomposer.ImageComposeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    images = {}
    for name, pk in (body.slots or {}).items():
        row = _rows_as_dicts(
            db, "SELECT s3_key FROM public.image_library_assets WHERE id = %s",
            (pk,))
        if not row:
            raise HTTPException(status_code=400,
                                detail=f"no such photo {pk} for slot '{name}'")
        try:
            images[name] = _download_pil(row[0]["s3_key"])
        except Exception as exc:
            raise HTTPException(status_code=502,
                                detail=f"could not fetch photo for '{name}': {exc}")

    texts = {k: str(v) for k, v in (body.texts or {}).items()}
    image_id = "IMG-" + uuid.uuid4().hex[:16]
    out_key = f"{_IMAGES_OUT_PREFIX}{image_id}.jpg"

    fd, tmp_path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        width, height = imgcomposer.render_to_file(template, images, texts, tmp_path)
        meta = _exporter.put_file(tmp_path, out_key, "image/jpeg", overwrite=False)
    except imgcomposer.ImageComposeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    recipe = {"template_id": body.template_id, "slots": body.slots or {},
              "texts": texts, "cityid": body.cityid, "topic": body.topic}
    db.execute_query("""
        INSERT INTO public.image_renders
          (image_id, template_id, state, cityid, topic, recipe, s3_key,
           width, height, size_bytes, checksum_sha256)
        VALUES (%(image_id)s, %(template_id)s, 'succeeded', %(cityid)s, %(topic)s,
                %(recipe)s::jsonb, %(key)s, %(w)s, %(h)s, %(size)s, %(sum)s)
    """, {"image_id": image_id, "template_id": body.template_id,
          "cityid": body.cityid, "topic": body.topic,
          "recipe": json.dumps(recipe), "key": out_key,
          "w": width, "h": height, "size": meta["size_bytes"],
          "sum": meta["checksum_sha256"]})

    try:
        url = _s3.presign(out_key)
    except Exception:
        url = None
    return {"image_id": image_id, "s3_key": out_key, "url": url,
            "width": width, "height": height, "size_bytes": meta["size_bytes"]}


@app.get("/admin/images")
def list_images(db=Depends(get_db), _=Depends(require_secret)):
    rows = _rows_as_dicts(db, """
        SELECT id, image_id, template_id, state, cityid, topic, s3_key,
               width, height, size_bytes, created_at
        FROM public.image_renders ORDER BY created_at DESC LIMIT 200
    """)
    for row in rows:
        row["created_at"] = str(row.get("created_at") or "")
        try:
            row["url"] = _s3.presign(row["s3_key"]) if row.get("s3_key") else None
        except Exception:
            row["url"] = None
    return {"images": rows}


@app.delete("/admin/images/{image_id}")
def delete_image(image_id: str, db=Depends(get_db), _=Depends(require_secret)):
    """Removes the record; the exported S3 object is untouched (no delete access)."""
    result = db.execute_query(
        "DELETE FROM public.image_renders WHERE image_id = %s", (image_id,))
    if result is False:
        raise HTTPException(status_code=409, detail="delete rejected")
    return {"ok": True}
