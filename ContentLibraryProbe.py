"""
Media measurement via ffprobe.

Technical properties are measured, never typed in by hand. Probing runs
against a presigned URL rather than a downloaded copy, matching the admin
backend, so inventory writes nothing to disk.

Running ffprobe and interpreting its output are separate functions on
purpose: parse_probe_json is pure, so the tests exercise the interesting
logic — rotation, rational frame rates, missing streams — against recorded
fixtures, with no ffprobe binary present.
"""

import json
import subprocess

FFPROBE_ARGS = [
    "-v", "error",
    "-print_format", "json",
    "-show_format",
    "-show_streams",
]


def probe(url, timeout=120, ffprobe="ffprobe"):
    """
    Measures one object. `url` is normally a presigned S3 URL.

    Returns the same shape as parse_probe_json. A failure is reported in
    the 'error' key rather than raised: a corrupt object is an asset to
    quarantine, not a reason to abandon the inventory run.
    """
    try:
        completed = subprocess.run(
            [ffprobe] + FFPROBE_ARGS + [url],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return _failure("ffprobe not installed")
    except subprocess.TimeoutExpired:
        return _failure(f"ffprobe timed out after {timeout}s")

    if completed.returncode != 0:
        # stderr can contain the presigned URL, which grants read access.
        # Record that it failed, not the signed URL.
        return _failure(f"ffprobe exited {completed.returncode}")

    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return _failure("ffprobe returned unparseable JSON")

    return parse_probe_json(data)


def parse_probe_json(data):
    """Turns raw ffprobe JSON into the columns the catalog stores."""
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        return _failure("no video stream", probe_data=data)

    coded_width = _as_int(video.get("width"))
    coded_height = _as_int(video.get("height"))
    rotation = rotation_of(video)

    # Stored dimensions are display dimensions, not coded ones. A phone
    # clip is frequently recorded 1920x1080 with a 90 degree rotation and
    # plays back as portrait; storing the coded pair would make it look
    # landscape to every consumer and let sideways footage into a 9:16
    # render. The coded values remain in probe_data.
    width, height = coded_width, coded_height
    if rotation in (90, 270) and coded_width and coded_height:
        width, height = coded_height, coded_width

    return {
        "duration_ms": _duration_ms(data, video),
        "width": width,
        "height": height,
        "rotation": rotation,
        "orientation": orientation_of(width, height),
        "frame_rate": _rational(video.get("avg_frame_rate"))
                      or _rational(video.get("r_frame_rate")),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "has_audio": audio is not None,
        "stream_count": len(streams),
        "probe_data": data,
        "error": None,
    }


def rotation_of(stream):
    """
    Display rotation in degrees, normalized to 0/90/180/270.

    ffmpeg reports this two ways depending on version: a 'rotate' tag on
    older builds, and a display matrix in side_data_list on newer ones,
    where the value is usually negative.
    """
    tag = (stream.get("tags") or {}).get("rotate")
    if tag is not None:
        return _normalize_degrees(tag)

    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            return _normalize_degrees(side_data["rotation"])
    return 0


def orientation_of(width, height):
    if not width or not height:
        return None
    if width == height:
        return "square"
    return "portrait" if height > width else "landscape"


def format_duration_display(duration_ms):
    """
    The legacy 'm:ss' form the admin tab shows. Kept in step with the
    backend's _probe_duration so the two never disagree.
    """
    if not duration_ms:
        return None
    minutes, seconds = divmod(int(round(duration_ms / 1000.0)), 60)
    return f"{minutes}:{seconds:02d}"


def _failure(message, probe_data=None):
    return {
        "duration_ms": None, "width": None, "height": None, "rotation": 0,
        "orientation": None, "frame_rate": None, "video_codec": None,
        "audio_codec": None, "has_audio": None, "stream_count": 0,
        "probe_data": probe_data, "error": message,
    }


def _duration_ms(data, video):
    for source in ((data.get("format") or {}).get("duration"), video.get("duration")):
        try:
            seconds = float(source)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return int(round(seconds * 1000))
    return None


def _rational(value):
    """Parses ffprobe's 'num/den' frame rates. '0/0' means unknown."""
    if not value:
        return None
    try:
        if "/" in str(value):
            numerator, denominator = str(value).split("/", 1)
            numerator, denominator = float(numerator), float(denominator)
            return round(numerator / denominator, 4) if denominator else None
        return round(float(value), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _normalize_degrees(value):
    try:
        return int(round(float(value))) % 360
    except (TypeError, ValueError):
        return 0


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
