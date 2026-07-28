"""
Recipe -> ffmpeg invocation -> validated MP4.

Command construction is a pure function of the recipe and the local input
paths, so the filter graph can be tested without ffmpeg installed. Only
run_ffmpeg and probe_output touch the system.

Every clip is normalized to the canvas before concatenation. Mixed source
dimensions, frame rates, sample rates and pixel formats are the usual cause
of a concat that produces a file which plays for two seconds and then
freezes, so nothing is assumed to already match.
"""

import json
import os
import random
import subprocess
import time
from typing import Dict, List, Optional

# Crockford base32, as ULID specifies: no I, L, O or U, so an id read aloud
# or typed from a screen cannot be ambiguous.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_render_id(now_ms=None, rng=None):
    """
    RND- plus a ULID: lexicographically sortable by creation time, with no
    marketing copy or user data leaking into an S3 key.
    """
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rng = rng or random.SystemRandom()

    def encode(value, length):
        out = []
        for _ in range(length):
            out.append(_CROCKFORD[value & 31])
            value >>= 5
        return "".join(reversed(out))

    randomness = rng.getrandbits(80)
    return f"RND-{encode(now_ms, 10)}{encode(randomness, 16)}"


def export_prefix(render_id, environment, when):
    """
    ugc-assets/exported/{env}/{yyyy}/{mm}/{dd}/{render-id}/

    Dated directories keep a listing usable as renders accumulate, and the
    render id makes the directory immutable by construction.
    """
    if environment not in ("dev", "staging", "prod"):
        raise ValueError(f"invalid environment: {environment!r}")
    return (f"ugc-assets/exported/{environment}/"
            f"{when:%Y}/{when:%m}/{when:%d}/{render_id}/")


def validate_recipe(recipe, schema_path=None):
    """
    Structural checks, plus the invariants a JSON Schema cannot express.

    Deliberately does not require the jsonschema package: these are the
    rules that actually break a render, and a hand-written check that runs
    everywhere beats a dependency that might not be installed on the
    render host.
    """
    errors = []

    if recipe.get("recipe_version") != 1:
        errors.append(f"unsupported recipe_version: {recipe.get('recipe_version')}")

    canvas = recipe.get("canvas") or {}
    for field in ("width", "height", "fps"):
        if not isinstance(canvas.get(field), int) or canvas[field] < 1:
            errors.append(f"canvas.{field} must be a positive integer")

    timeline = recipe.get("timeline") or []
    if not timeline:
        errors.append("timeline is empty")

    expected_at, seen_pks, seen_checksums = 0, set(), set()
    for index, clip in enumerate(timeline):
        where = f"timeline[{index}]"
        source_in = clip.get("source_in_ms")
        source_out = clip.get("source_out_ms")
        if not isinstance(source_in, int) or source_in < 0:
            errors.append(f"{where}.source_in_ms must be >= 0")
        if not isinstance(source_out, int) or (isinstance(source_in, int)
                                               and source_out <= source_in):
            errors.append(f"{where}.source_out_ms must be greater than source_in_ms")
        if not clip.get("s3_key"):
            errors.append(f"{where}.s3_key is required")
        # Without a checksum the render cannot prove it used the footage the
        # recipe named, which is the whole point of recording lineage.
        if not clip.get("checksum_sha256"):
            errors.append(f"{where}.checksum_sha256 is required")

        if clip.get("timeline_in_ms") != expected_at:
            errors.append(f"{where}.timeline_in_ms is {clip.get('timeline_in_ms')}, "
                          f"expected {expected_at} — timeline must be contiguous")
        if isinstance(source_in, int) and isinstance(source_out, int):
            expected_at += max(0, source_out - source_in)

        pk = clip.get("asset_pk")
        if pk in seen_pks:
            errors.append(f"{where}: asset {pk} appears more than once")
        seen_pks.add(pk)

        checksum = clip.get("checksum_sha256")
        if checksum and checksum in seen_checksums:
            errors.append(f"{where}: duplicate footage — checksum already used "
                          f"in this render under a different key")
        if checksum:
            seen_checksums.add(checksum)

    if recipe.get("total_duration_ms") != expected_at:
        errors.append(f"total_duration_ms is {recipe.get('total_duration_ms')}, "
                      f"but the timeline sums to {expected_at}")
    return errors


def build_filter_graph(recipe, has_audio_flags, loudness_lufs=-14.0,
                       drawtext_clauses=None, music=None):
    """
    Normalizes every clip to the canvas, concatenates, then normalizes
    loudness — all inside the one graph.

    scale with force_original_aspect_ratio=increase followed by a centre
    crop fills a 9:16 frame without letterboxing. setsar=1 matters more
    than it looks: a non-square pixel aspect ratio on one input makes
    concat refuse the whole graph.

    Clips without audio get silence rather than being skipped, because
    concat with a=1 requires every segment to have both streams.

    loudnorm belongs here rather than as an -af flag: ffmpeg refuses to
    apply simple filtering to a stream fed from a complex filtergraph, and
    fails with "Simple and complex filtering cannot be used together for
    the same stream" — which reads like a problem with the audio rather
    than with where the filter was attached.
    """
    canvas = recipe["canvas"]
    width, height, fps = canvas["width"], canvas["height"], canvas["fps"]
    parts, labels = [], []
    silence_index = len(recipe["timeline"])

    for index, _clip in enumerate(recipe["timeline"]):
        parts.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p[v{index}]")
        if has_audio_flags[index]:
            parts.append(f"[{index}:a]aresample=48000,aformat="
                         f"sample_fmts=fltp:channel_layouts=stereo[a{index}]")
        else:
            parts.append(f"[{silence_index}:a]aformat="
                         f"sample_fmts=fltp:channel_layouts=stereo[a{index}]")
        labels.append(f"[v{index}][a{index}]")

    # Text is drawn after the concat, so a caption's timing is measured
    # against the finished timeline rather than against whichever clip
    # happens to be under it — which is what lets a line span a cut.
    if drawtext_clauses:
        parts.append(f"{''.join(labels)}concat=n={len(recipe['timeline'])}:v=1:a=1[cutv][cata]")
        parts.append(f"[cutv]{','.join(drawtext_clauses)}[outv]")
    else:
        parts.append(f"{''.join(labels)}concat=n={len(recipe['timeline'])}:v=1:a=1[outv][cata]")

    # Music bed, when one was selected. The clip audio is ambient (restaurant
    # noise, not speech), so it is ducked under the track rather than the
    # other way round. amix duration=first ends the mix with the video; the
    # music input is looped upstream (-stream_loop) so a short track fills the
    # whole cut. normalize=0 keeps amix from halving each input by count —
    # loudnorm sets the final level. loudnorm still lives in the graph, since
    # ffmpeg refuses simple -af filtering on a complex-graph stream.
    if music:
        source_gain = music.get("source_gain", 0.28)
        music_gain = music.get("gain", 0.85)
        parts.append(f"[cata]volume={source_gain}[abed]")
        parts.append(f"[{music['index']}:a]aresample=48000,"
                     f"aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"volume={music_gain}[amus]")
        parts.append("[abed][amus]amix=inputs=2:duration=first:"
                     "dropout_transition=0:normalize=0[amixed]")
        parts.append(f"[amixed]loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11[outa]")
    else:
        parts.append(f"[cata]loudnorm=I={loudness_lufs}:TP=-1.5:LRA=11[outa]")
    return ";".join(parts)


def build_ffmpeg_command(recipe, input_paths, output_path, ffmpeg="ffmpeg",
                         has_audio_flags=None, loudness_lufs=-14.0,
                         drawtext_clauses=None, music_path=None, music_mix=None):
    """
    Returns the argv for one render. Pure: no filesystem, no subprocess.

    -ss and -t are placed before -i so ffmpeg seeks on input rather than
    decoding and discarding everything up to the trim point. With
    re-encoding that stays frame-accurate.
    """
    timeline = recipe["timeline"]
    if len(input_paths) != len(timeline):
        raise ValueError(f"{len(input_paths)} inputs for {len(timeline)} clips")
    if has_audio_flags is None:
        has_audio_flags = [
            (clip.get("audio_policy") or {}).get("mode") != "silent"
            for clip in timeline
        ]

    args = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
    for clip, path in zip(timeline, input_paths):
        start = clip["source_in_ms"] / 1000.0
        take = (clip["source_out_ms"] - clip["source_in_ms"]) / 1000.0
        args += ["-ss", f"{start:.3f}", "-t", f"{take:.3f}", "-i", path]

    # Silence source for clips with no audio track, referenced by the graph.
    args += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    # Music bed, if any. -stream_loop -1 loops a track shorter than the video;
    # amix duration=first trims a longer one. Added after the clips and the
    # silence source, so its filter index is len(timeline) + 1.
    music = None
    if music_path:
        args += ["-stream_loop", "-1", "-i", music_path]
        music = {
            "index": len(timeline) + 1,
            "gain": (music_mix or {}).get("gain", 0.85),
            "source_gain": (music_mix or {}).get("source_gain", 0.28),
        }

    # Loudness normalization lives inside the filter graph, not in an -af
    # flag: ffmpeg rejects simple filtering on a stream that comes out of
    # -filter_complex. Social platforms normalize on upload anyway, so
    # matching their target keeps the master from being pulled down twice.
    args += [
        "-filter_complex", build_filter_graph(recipe, has_audio_flags,
                                              loudness_lufs=loudness_lufs,
                                              drawtext_clauses=drawtext_clauses,
                                              music=music),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0",
        "-r", str(recipe["canvas"]["fps"]),
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
        output_path,
    ]
    return args


def build_thumbnail_command(source_path, output_path, at_ms=1000, ffmpeg="ffmpeg"):
    return [ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-ss", f"{at_ms / 1000.0:.3f}", "-i", source_path,
            "-frames:v", "1", "-q:v", "3", output_path]


def build_preview_command(source_path, output_path, height=960, ffmpeg="ffmpeg"):
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", source_path,
            "-vf", f"scale=-2:{height}", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "30", "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart", output_path]


def run_ffmpeg(args, timeout=1800):
    """Runs one command. stderr is returned, never logged wholesale — it can
    contain presigned URLs."""
    completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return completed.returncode, completed.stderr[-4000:]


def validate_output(probe_result, recipe, duration_tolerance_ms=750):
    """
    Technical QA on the rendered file, against what the recipe promised.

    Checks the delivery contract rather than taste: dimensions, frame rate,
    codecs, and that the duration matches the timeline. A render that
    silently comes out three seconds short is the failure this catches.
    """
    canvas = recipe["canvas"]
    failures = []

    if probe_result.get("error"):
        return {"passed": False, "failures": [f"probe failed: {probe_result['error']}"]}

    if probe_result.get("width") != canvas["width"] or \
            probe_result.get("height") != canvas["height"]:
        failures.append(
            f"expected {canvas['width']}x{canvas['height']}, "
            f"got {probe_result.get('width')}x{probe_result.get('height')}")

    fps = probe_result.get("frame_rate")
    if fps is None or abs(fps - canvas["fps"]) > 0.5:
        failures.append(f"expected {canvas['fps']} fps, got {fps}")

    if probe_result.get("video_codec") != "h264":
        failures.append(f"expected h264, got {probe_result.get('video_codec')}")
    if probe_result.get("audio_codec") != "aac":
        failures.append(f"expected aac, got {probe_result.get('audio_codec')}")

    expected_ms = recipe["total_duration_ms"]
    actual_ms = probe_result.get("duration_ms") or 0
    if abs(actual_ms - expected_ms) > duration_tolerance_ms:
        failures.append(
            f"duration {actual_ms}ms differs from the recipe's {expected_ms}ms "
            f"by more than {duration_tolerance_ms}ms")

    return {
        "passed": not failures,
        "failures": failures,
        "measured": {
            "width": probe_result.get("width"), "height": probe_result.get("height"),
            "frame_rate": fps, "duration_ms": actual_ms,
            "video_codec": probe_result.get("video_codec"),
            "audio_codec": probe_result.get("audio_codec"),
        },
    }


def build_manifest(render_id, environment, recipe, artifacts, sources,
                   created_at, tools=None):
    """
    The record of what was made, from what, with which tools.

    Its purpose is answering "what exactly is in this video" years later,
    so it pins every source by checksum and version id rather than by key
    alone — a key can be overwritten, a checksum cannot.
    """
    return {
        "schema_version": 1,
        "render_id": render_id,
        "environment": environment,
        "created_at": created_at,
        "brief": recipe.get("brief", {}),
        "template": recipe.get("template", {}),
        "recipe": recipe,
        "sources": [
            {
                "asset_id": s.get("asset_id"),
                "asset_pk": s.get("asset_pk"),
                "bucket": s.get("bucket"),
                "s3_key": s.get("s3_key"),
                "s3_version_id": s.get("s3_version_id"),
                "checksum_sha256": s.get("checksum_sha256"),
                "source_in_ms": s.get("source_in_ms"),
                "source_out_ms": s.get("source_out_ms"),
            }
            for s in sources
        ],
        "tools": tools or {},
        "artifacts": [
            {"role": a["role"], "s3_key": a["s3_key"],
             "size_bytes": a.get("size_bytes"),
             "checksum_sha256": a.get("checksum_sha256")}
            for a in artifacts
        ],
    }
