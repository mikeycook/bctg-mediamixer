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

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.db import get_db  # noqa: E402
from S3Interpreter import S3Interpreter  # noqa: E402
import ContentLibraryPaths as clpaths  # noqa: E402
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
    rows = db.execute_query("SELECT count(*) FROM public.content_library_assets")
    return {"ok": True, "assets": rows[0][0] if rows else None}


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

    out = []
    for row in rows:
        row["created_at"] = str(row.get("created_at") or "")
        row["updated_at"] = str(row.get("updated_at") or "")
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


class ContentLibraryUpdate(BaseModel):
    values: Dict[str, Any]


@app.put("/admin/content-library/{asset_pk}")
def update_content_library(asset_pk: int, body: ContentLibraryUpdate,
                           db=Depends(get_db), _=Depends(require_secret)):
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
