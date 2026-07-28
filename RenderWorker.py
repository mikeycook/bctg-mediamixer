#!/usr/bin/env python3
"""
RenderWorker.py

Brief -> recipe -> rendered MP4 -> ugc-assets/exported/ -> lineage rows.

Ties together ContentLibrarySelect (what to use), VideoRenderer (how to
build it), and S3Exporter (where it may be written). Everything with
interesting logic lives in those modules; this is the part that touches
the world, in the order that keeps it recoverable.

The ordering matters more than it looks:

  Sources are verified against their recorded SHA-256 before rendering. A
  mismatch means the object changed since it was catalogued, and rendering
  from it would produce a video whose manifest is a lie. That aborts.

  The render is only marked succeeded after every artifact is uploaded and
  every lineage row commits. A crash anywhere earlier leaves it 'failed'
  or 'rendering' with the exported directory possibly incomplete — which
  is safe, because a retry uses a new render id and never overwrites.

  Scratch is deleted in a finally block. This host also carries the
  database and the API; a render that leaves 30 GB behind takes both down
  with it eventually.

Usage:
    python3 RenderWorker.py --brief-file brief.json --dry-run
    python3 RenderWorker.py --brief '{"cityid":"CIT-00000000002","topic":"pizza"}'
    python3 RenderWorker.py --brief-file brief.json --environment dev --keep-scratch

Env (from the systemd EnvironmentFile in the .service, or ./.env locally):
    DATABASE_URL   — required
    CLIPS_BUCKET   — default big-city-travel-guide-clips
    CLIPS_REGION   — default us-east-1
    SCRATCH_DIR    — default /opt/mediamixer/scratch
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse, unquote

import Attribution as attribution
import CaptionBuilder as captions
import ContentLibraryProbe as clprobe
import ContentLibrarySelect as clselect
import VideoRenderer as vr
from PostgresInterpreter import PostgresInterpreter
from S3Exporter import S3Exporter, sha256_file
from S3Interpreter import S3Interpreter

DEFAULT_SCRATCH = "/opt/mediamixer/scratch"

# A render needs room for its sources, an encode, a preview and a thumbnail.
# Refusing up front beats failing mid-encode with a half-written file and a
# disk that is now also too full to clean up comfortably.
MIN_FREE_BYTES = 2 * 1024 ** 3


class RenderFailure(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


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
    if not u.username or not (u.path or "").lstrip("/"):
        raise ValueError("Could not parse user/database from DATABASE_URL")
    return {"user": u.username, "password": unquote(u.password or ""),
            "host": u.hostname or "127.0.0.1", "port": str(u.port or 5432),
            "database": (u.path or "").lstrip("/")}


def execute_returning(db, sql, params):
    rows = db.execute_query(sql, params)
    if rows is False:
        return None
    db.connection.commit()
    return rows


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------

def check_free_space(path, needed=MIN_FREE_BYTES):
    free = shutil.disk_usage(path).free
    if free < needed:
        raise RenderFailure(
            "render_failed",
            f"only {free / 1024**3:.1f} GiB free at {path}, need "
            f"{needed / 1024**3:.1f} GiB — refusing to start")
    return free


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def download_sources(s3, recipe, workdir, verify=True):
    """
    Fetches each clip and verifies it against the checksum in the recipe.

    A mismatch aborts the render. The alternative — carrying on with
    whatever the object now contains — produces a video whose manifest
    claims sources it did not actually use, which is worse than no video.
    """
    paths = []
    for index, clip in enumerate(recipe["timeline"]):
        local = os.path.join(workdir, f"src{index:02d}_{os.path.basename(clip['s3_key'])}")
        with open(local, "wb") as handle:
            for chunk in s3.iter_object(clip["s3_key"]):
                handle.write(chunk)

        if verify:
            actual = sha256_file(local)
            expected = clip.get("checksum_sha256")
            if expected and actual != expected:
                raise RenderFailure(
                    "source_missing",
                    f"{clip['s3_key']} has changed since it was catalogued "
                    f"(expected {expected[:12]}…, got {actual[:12]}…)")
        paths.append(local)
    return paths


# ---------------------------------------------------------------------------
# Database lineage
# ---------------------------------------------------------------------------

def create_render(db, render_id, brief, recipe, environment):
    rows = execute_returning(db, """
        INSERT INTO public.content_library_renders (
            render_id, environment, state, cityid, topic, platform,
            template_id, template_version, target_duration_ms, brief, recipe,
            selection_seed, renderer_name, started_at
        ) VALUES (
            %(render_id)s, %(environment)s, 'rendering', %(cityid)s, %(topic)s,
            %(platform)s, %(template_id)s, %(template_version)s,
            %(target_ms)s, %(brief)s, %(recipe)s, %(seed)s, 'ffmpeg', now()
        ) RETURNING id
    """, {
        "render_id": render_id, "environment": environment,
        "cityid": brief.cityid, "topic": brief.topic,
        "platform": list(brief.platforms),
        "template_id": recipe["template"]["id"],
        "template_version": recipe["template"]["version"],
        "target_ms": brief.target_duration_ms,
        "brief": json.dumps(recipe["brief"]), "recipe": json.dumps(recipe),
        "seed": brief.seed,
    })
    if not rows:
        raise RenderFailure("render_failed", "could not create the render row")
    return rows[0][0]


def record_render_assets(db, render_pk, recipe):
    for sequence, clip in enumerate(recipe["timeline"]):
        db.execute_query("""
            INSERT INTO public.content_library_render_assets (
                render_id, asset_id, sequence_no, role, source_in_ms,
                source_out_ms, timeline_in_ms, transform, audio_policy
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (render_id, sequence_no) DO NOTHING
        """, (render_pk, clip["asset_pk"], sequence, clip["role"],
              clip["source_in_ms"], clip["source_out_ms"], clip["timeline_in_ms"],
              json.dumps(clip.get("transform") or {}),
              json.dumps(clip.get("audio_policy") or {})))


def record_render_music(db, render_pk, tracks):
    """
    Lineage: which music a render credited, so a published video's
    attribution can be reconstructed from the render alone. Best-effort — a
    track dict that carries its catalog id is recorded, one without is
    skipped, so this is harmless until music selection populates ids.
    """
    seq = 0
    for track in tracks or []:
        track_pk = track.get("track_pk") or track.get("id")
        if not track_pk:
            continue
        db.execute_query("""
            INSERT INTO public.content_library_render_music
                (render_id, track_id, sequence_no, role)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (render_id, sequence_no) DO NOTHING
        """, (render_pk, track_pk, seq, track.get("role", "bed")))
        seq += 1


def record_artifacts(db, render_pk, artifacts):
    for artifact in artifacts:
        db.execute_query("""
            INSERT INTO public.content_library_render_artifacts (
                render_id, role, bucket_name, s3_key, size_bytes,
                content_type, checksum_sha256
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (bucket_name, s3_key) DO NOTHING
        """, (render_pk, artifact["role"], artifact["bucket_name"],
              artifact["s3_key"], artifact.get("size_bytes"),
              artifact.get("content_type"), artifact.get("checksum_sha256")))


def finish_render(db, render_pk, state, actual_ms=None, renderer_version=None,
                  error_code=None, error_detail=None):
    db.execute_query("""
        UPDATE public.content_library_renders
        SET state = %(state)s, actual_duration_ms = %(actual_ms)s,
            renderer_version = COALESCE(%(rv)s, renderer_version),
            error_code = %(code)s, error_detail = %(detail)s,
            completed_at = now()
        WHERE id = %(pk)s
    """, {"pk": render_pk, "state": state, "actual_ms": actual_ms,
          "rv": renderer_version, "code": error_code,
          "detail": (error_detail or "")[:2000] or None})


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def ffmpeg_version(ffmpeg="ffmpeg"):
    import subprocess
    try:
        out = subprocess.run([ffmpeg, "-version"], capture_output=True,
                             text=True, timeout=15)
        return (out.stdout or "").splitlines()[0].strip() or None
    except Exception:
        return None


def plan_and_write_captions(db, recipe, workdir, overrides=None):
    """
    Resolves captions and writes the text files drawtext will read.

    Planning is shared with the preview endpoint, so what the operator saw
    before rendering is what gets burned in.
    """
    plan = captions.plan_for_recipe(db, recipe, overrides)
    for problem in plan.unresolved:
        print(f"[CAPT] skipped — {problem}")

    caption_list = list(plan.captions)

    # A music bed that requires attribution earns a short credit at the end,
    # for the viewers who never open the description. The full credit still
    # goes to attribution.txt; this is the courtesy copy. Runs through the
    # same wrap/fit/drawtext path as every other caption.
    outro = attribution.burn_in_credit(recipe.get("music"))
    total = int(recipe.get("total_duration_ms") or 0)
    if outro and total > 0:
        caption_list.append(captions.Caption(
            text=outro, start_ms=max(0, total - 3000), end_ms=total, style="label"))
        caption_list.sort(key=lambda c: c.start_ms)

    if not caption_list:
        return [], []

    prepared = captions.write_caption_files(caption_list, workdir, recipe["canvas"])
    font = captions.find_font()
    if font is None:
        print("[CAPT] no usable font found; install fonts-dejavu-core. "
              "Rendering without text.")
        return [], []

    clauses = captions.drawtext_filters(prepared, recipe["canvas"], fontfile=font)
    for caption in caption_list:
        print(f"[CAPT] {caption.start_ms / 1000:5.1f}s  {caption.style:<6} "
              f"{caption.text}")
    return clauses, caption_list


def download_music(s3, recipe, workdir):
    """
    Fetches the music bed, if the recipe selected one. Returns (path, mix) or
    (None, None). No checksum verification: unlike a source clip, a bed swap
    is not a provenance problem — the credit is bound to the track chosen at
    selection, which is what attribution.txt and the lineage record.
    """
    mix = (recipe.get("audio_mix") or {}).get("music")
    if not mix or not mix.get("s3_key"):
        return None, None
    local = os.path.join(workdir, "music_" + os.path.basename(mix["s3_key"]))
    with open(local, "wb") as handle:
        for chunk in s3.iter_object(mix["s3_key"]):
            handle.write(chunk)
    print(f"[MUS ] bed {mix['s3_key']}")
    return local, mix


_FFMPEG_ERROR_HINTS = (
    "error", "invalid", "unable", "no such", "failed", "cannot", "denied",
    "not found", "does not", "no space", "out of memory", "killed",
    "conversion failed", "buffer", "overflow", "matches no streams",
)


def _ffmpeg_error(code, stderr):
    """
    The informative part of an ffmpeg failure.

    ffmpeg ends with libx264's statistics even on some failures, so the tail
    is often useless. Pull the lines that name a cause; fall back to the tail
    only when none are found.
    """
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    flagged = [ln for ln in lines
               if any(h in ln.lower() for h in _FFMPEG_ERROR_HINTS)]
    detail = " | ".join(flagged[-6:]) if flagged else (stderr or "")[-800:]
    return f"ffmpeg exited {code}: {detail}"


def render_artifacts(recipe, input_paths, workdir, ffmpeg="ffmpeg", timeout=1800,
                     drawtext_clauses=None, music_path=None, music_mix=None):
    """Produces final.mp4, preview.mp4 and thumbnail.jpg locally."""
    final = os.path.join(workdir, "final.mp4")
    code, stderr = vr.run_ffmpeg(
        vr.build_ffmpeg_command(recipe, input_paths, final, ffmpeg=ffmpeg,
                                drawtext_clauses=drawtext_clauses,
                                music_path=music_path, music_mix=music_mix),
        timeout=timeout)
    if code != 0 or not os.path.exists(final):
        # Print the whole thing so journalctl has it, and raise with the lines
        # that actually explain the failure — ffmpeg's last 800 chars are
        # usually libx264's encode statistics, not the error.
        print(f"[FFMPEG] failed ({code}); full stderr follows:\n{stderr}")
        raise RenderFailure("render_failed", _ffmpeg_error(code, stderr))

    made = [("final", final, "video/mp4")]

    preview = os.path.join(workdir, "preview.mp4")
    if vr.run_ffmpeg(vr.build_preview_command(final, preview, ffmpeg=ffmpeg),
                     timeout=timeout)[0] == 0 and os.path.exists(preview):
        made.append(("preview", preview, "video/mp4"))

    thumb = os.path.join(workdir, "thumbnail.jpg")
    if vr.run_ffmpeg(vr.build_thumbnail_command(final, thumb, ffmpeg=ffmpeg),
                     timeout=120)[0] == 0 and os.path.exists(thumb):
        made.append(("thumbnail", thumb, "image/jpeg"))

    # Preview and thumbnail are recommended, not required — a render is
    # still delivered if only the master survives.
    return final, made


def run(db, s3, exporter, brief, environment, scratch_root, ffmpeg="ffmpeg",
        keep_scratch=False, verify_sources=True):
    recipe = clselect.select(db, brief)

    errors = vr.validate_recipe(recipe)
    if errors:
        raise RenderFailure("recipe_invalid", "; ".join(errors))

    render_id = vr.new_render_id()
    now = datetime.now(timezone.utc)
    prefix = vr.export_prefix(render_id, environment, now)

    os.makedirs(scratch_root, exist_ok=True)
    check_free_space(scratch_root)
    workdir = tempfile.mkdtemp(prefix=f"{render_id}_", dir=scratch_root)

    render_pk = create_render(db, render_id, brief, recipe, environment)
    print(f"[RENDER] {render_id}  -> s3://{exporter.bucket}/{prefix}")

    try:
        record_render_assets(db, render_pk, recipe)

        print(f"[SRC ] fetching {len(recipe['timeline'])} source(s)")
        inputs = download_sources(s3, recipe, workdir, verify=verify_sources)

        clauses, planned = plan_and_write_captions(db, recipe, workdir,
                                                  brief.caption_overrides)
        recipe["captions"] = [c.as_dict() for c in planned]

        music_path, music_mix = download_music(s3, recipe, workdir)

        print("[FFMPEG] encoding")
        final, made = render_artifacts(recipe, inputs, workdir, ffmpeg=ffmpeg,
                                       drawtext_clauses=clauses,
                                       music_path=music_path, music_mix=music_mix)

        if planned:
            srt = os.path.join(workdir, "captions.srt")
            with open(srt, "w", encoding="utf-8") as handle:
                handle.write(captions.to_srt(planned))
            made.append(("captions", srt, "application/x-subrip"))

        # Music credits the licence requires: the authoritative full copy,
        # written beside the video for the publisher to paste into the post.
        credit = attribution.attribution_text(recipe.get("music"))
        if credit:
            apath = os.path.join(workdir, "attribution.txt")
            with open(apath, "w", encoding="utf-8") as handle:
                handle.write(credit)
            made.append(("attribution", apath, "text/plain"))
            record_render_music(db, render_pk, recipe.get("music"))

        probe = clprobe.probe(final)
        validation = vr.validate_output(probe, recipe)
        print(f"[QA  ] {'passed' if validation['passed'] else 'FAILED'}"
              f"  {validation['measured']}")
        if not validation["passed"]:
            raise RenderFailure("validation_failed", "; ".join(validation["failures"]))

        uploaded = []
        for role, path, content_type in made:
            key = prefix + os.path.basename(path)
            uploaded.append({**exporter.put_file(path, key, content_type=content_type),
                             "role": role})
            print(f"[PUT ] {role:<10} {key}")

        manifest = vr.build_manifest(
            render_id, environment, recipe, uploaded,
            [{**c, "bucket": exporter.bucket} for c in recipe["timeline"]],
            created_at=now.isoformat(),
            tools={"renderer": "ffmpeg", "renderer_version": ffmpeg_version(ffmpeg)})

        uploaded.append({**exporter.put_bytes(
            json.dumps(manifest, indent=2, default=str), prefix + "manifest.json"),
            "role": "manifest"})
        uploaded.append({**exporter.put_bytes(
            json.dumps(validation, indent=2, default=str), prefix + "validation.json"),
            "role": "validation"})

        record_artifacts(db, render_pk, uploaded)
        finish_render(db, render_pk, "succeeded",
                      actual_ms=probe.get("duration_ms"),
                      renderer_version=ffmpeg_version(ffmpeg))
        db.connection.commit()

        print(f"[DONE] {render_id}  {probe.get('duration_ms')}ms  "
              f"{len(uploaded)} artifact(s)")
        return {"render_id": render_id, "prefix": prefix,
                "artifacts": uploaded, "validation": validation}

    except RenderFailure as failure:
        finish_render(db, render_pk, "failed", error_code=failure.code,
                      error_detail=failure.detail)
        db.connection.commit()
        raise
    except Exception as exc:
        finish_render(db, render_pk, "failed", error_code="render_failed",
                      error_detail=str(exc))
        db.connection.commit()
        raise
    finally:
        if keep_scratch:
            print(f"[KEEP] {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Render one video from a brief.")
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--brief", help="Brief as inline JSON")
    source.add_argument("--brief-file", help="Path to a brief JSON file")
    ap.add_argument("--environment", default="dev", choices=["dev", "staging", "prod"])
    ap.add_argument("--dry-run", action="store_true",
                    help="Select and validate a recipe, render nothing")
    ap.add_argument("--keep-scratch", action="store_true",
                    help="Leave the working directory in place for inspection")
    ap.add_argument("--no-verify-sources", action="store_true",
                    help="Skip source checksum verification (not recommended)")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--scratch-dir", default=os.getenv("SCRATCH_DIR", DEFAULT_SCRATCH))
    args = ap.parse_args()

    load_env_file()
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set.")

    raw = json.loads(args.brief) if args.brief else \
        json.loads(open(args.brief_file, encoding="utf-8").read())
    raw.setdefault("environment", args.environment)
    brief = clselect.VideoBrief.from_dict(raw)

    bucket = os.getenv("CLIPS_BUCKET", "big-city-travel-guide-clips")
    region = os.getenv("CLIPS_REGION", "us-east-1")

    db = PostgresInterpreter(**parse_database_url(database_url))
    with db:
        if not db.connection:
            raise SystemExit("Could not connect to the database.")

        if args.dry_run:
            try:
                recipe = clselect.select(db, brief)
            except clselect.SelectionError as failure:
                print(json.dumps(failure.as_dict(), indent=2))
                raise SystemExit(1)
            errors = vr.validate_recipe(recipe)
            print(json.dumps(recipe, indent=2, default=str))
            print(f"\nvalidation: {'clean' if not errors else errors}")
            print("Would be rendered. No files were written and no rows changed.")
            return

        try:
            run(db, S3Interpreter(bucket, region=region),
                S3Exporter(bucket, region=region), brief, args.environment,
                args.scratch_dir, ffmpeg=args.ffmpeg,
                keep_scratch=args.keep_scratch,
                verify_sources=not args.no_verify_sources)
        except clselect.SelectionError as failure:
            print(json.dumps(failure.as_dict(), indent=2))
            raise SystemExit(1)
        except RenderFailure as failure:
            print(f"[FAIL] {failure.code}: {failure.detail}")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
