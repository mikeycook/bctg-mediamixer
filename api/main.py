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

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import get_db  # noqa: E402
from S3Interpreter import S3Interpreter  # noqa: E402
import ContentLibraryPaths as clpaths  # noqa: E402
import CaptionBuilder as captions  # noqa: E402
import ContentLibrarySelect as clselect  # noqa: E402
import ContentLibrarySync as sync  # noqa: E402

app = FastAPI(title="MediaMixer Content Library")

_BUCKET = os.getenv("CLIPS_BUCKET", "big-city-travel-guide-clips")
_PREFIX = os.getenv("CLIPS_PREFIX", "ugc-assets/")
_REGION = os.getenv("CLIPS_REGION", "us-east-1")
_ADMIN_SECRET = os.getenv("MEDIAMIXER_ADMIN_SECRET", "")

_s3 = S3Interpreter(_BUCKET, region=_REGION)

# Columns a reviewer may write. The governance fields are here so an asset
# can be activated through the tab that already exists, rather than needing
# new UI before anything can become eligible.
_EDITABLE = {
    "asset_id", "place_name", "cityid", "country", "type", "subtype",
    "category", "subcategory", "duration", "hook_compatibility", "notes",
    "status", "rights_status", "rights_source", "city_agnostic",
    "shot_type", "camera_motion", "time_of_day", "quality_score",
}

_OPTION_FIELDS = ("type", "subtype", "category", "subcategory", "country",
                  "asset_type", "status", "rights_status")


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

    # Response shape matches the admin backend's, so the tab's handler is
    # unchanged. Durations come from the scheduled job now, hence zero.
    return {"added": added, "durations": 0, "listed": len(objects)}


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
    existing = {r[0] for r in
                (db.execute_query(
                    "SELECT s3_key FROM public.content_library_music_tracks") or [])}
    added, probed, listed = 0, 0, 0
    try:
        for obj in _s3.list_objects(_MUSIC_PREFIX):
            key, size = obj["key"], obj["size"]
            if key.endswith("/") or size == 0:
                continue
            if not key.lower().endswith(_AUDIO_EXT):
                continue
            listed += 1
            if key in existing:
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
    return {"added": added, "probed": probed, "listed": listed}


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


@app.post("/admin/renders")
def start_render(body: BriefBody, db=Depends(get_db), _=Depends(require_secret)):
    """
    Starts a render in the background and returns immediately.

    An encode takes minutes, which is far longer than an HTTP request should
    hold. Selection runs synchronously first so an unfillable brief fails
    here with a useful message instead of appearing to start and then dying
    in a log the operator never sees.
    """
    brief = _brief_from(body.brief)
    try:
        clselect.select(db, brief)
    except clselect.SelectionError as failure:
        raise HTTPException(status_code=422, detail=failure.as_dict())

    # Pre-flight the scratch directory here rather than letting the worker
    # discover it. The worker creates its render row only after setting up a
    # workspace, so a failure at this stage leaves no trace anywhere — the
    # render simply never appears, which is the least debuggable outcome
    # available.
    scratch = os.getenv("SCRATCH_DIR", "/opt/mediamixer/scratch")
    if not os.path.isdir(scratch) or not os.access(scratch, os.W_OK):
        raise HTTPException(
            status_code=503,
            detail=f"scratch directory {scratch} is not writable by this "
                   f"service. If it exists and is owned correctly, the unit "
                   f"needs ReadWritePaths={scratch} — systemd's sandbox is "
                   f"inherited by the render worker.")

    environment = body.brief.get("environment", "dev")
    args = [_PYTHON, str(_REPO_ROOT / "RenderWorker.py"),
            "--brief", json.dumps(body.brief), "--environment", environment]
    try:
        # stdio is inherited so the worker's output lands in this service's
        # journal. Discarding it makes an early crash completely silent.
        subprocess.Popen(args, cwd=str(_REPO_ROOT), start_new_session=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not start render: {exc}")

    # The worker creates its own render row within a second or two; the UI
    # picks it up by polling rather than being told an id that does not
    # exist yet.
    return {"started": True, "environment": environment}


@app.get("/admin/renders")
def list_renders(limit: int = Query(25, ge=1, le=200), db=Depends(get_db),
                 _=Depends(require_secret)):
    rows = _rows_as_dicts(db, """
        SELECT r.id, r.render_id, r.state, r.environment, r.cityid, r.topic,
               r.template_id, r.target_duration_ms, r.actual_duration_ms,
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
