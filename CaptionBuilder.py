"""
Editorial text over the video: hook lines, place names, calls to action.

Not speech subtitles. This footage is ambient restaurant noise, so there is
nothing to transcribe; what a Reel needs is a line on screen in the first
two seconds and a call to action at the end.

Copy comes from patterns in the template, filled from catalog facts about
the clips selection actually chose. Deterministic, needs no model, and
structurally cannot invent a claim — a pattern can only interpolate a field
that exists. The design package is firm that generated copy must draw on
verified facts, and the simplest way to guarantee that is to have nothing
else available.

Three things here are less obvious than they look.

Escaping. ffmpeg's drawtext treats ':', '\\', '%' and single quotes as
special, so "Roberta's" and "L'Industrie" — two of the most-used places in
this library — break a naive command. Text is written to a file and passed
with textfile=, which sidesteps the problem instead of trying to out-escape
it.

Wrapping. drawtext does not wrap. A long line runs off the side of the
frame with no warning, so lines are broken here before ffmpeg sees them.

Safe areas. Instagram and TikTok draw their own UI over the bottom fifth of
the frame and up the right edge. Text positioned by eye on a desktop
preview disappears behind platform chrome on a phone, so the safe band is a
rule in code rather than a judgement per video.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Fractions of frame height. Platform UI covers roughly the bottom fifth;
# the top ~8% catches status bars and the app's own header.
SAFE_TOP = 0.10
SAFE_BOTTOM = 0.78

# Horizontal safe margin on each side. A caption is centered on its text
# width, so without this a wide line touches — or crosses — the frame edge.
SAFE_SIDE = 0.06

# Rough advance width per character at a given font size, for the bold sans
# drawtext uses. Deliberately on the wide side: over-estimating only forces
# an earlier line break, which costs nothing, whereas under-estimating lets
# a line run off the frame, which costs the shot.
CHAR_WIDTH_RATIO = 0.60

STYLES = {
    "hook": {
        "font_size_ratio": 0.058,   # of frame height
        "position": "upper",
        "box_opacity": 0.55,
        "max_lines": 3,
    },
    "label": {
        "font_size_ratio": 0.038,
        "position": "lower",
        "box_opacity": 0.5,
        "max_lines": 2,
    },
    "cta": {
        "font_size_ratio": 0.050,
        "position": "center",
        "box_opacity": 0.6,
        "max_lines": 2,
    },
}


@dataclass
class Caption:
    text: str
    start_ms: int
    end_ms: int
    style: str = "label"

    def as_dict(self):
        return {"text": self.text, "start_ms": self.start_ms,
                "end_ms": self.end_ms, "style": self.style}


@dataclass
class CaptionPlan:
    captions: List[Caption] = field(default_factory=list)
    unresolved: List[str] = field(default_factory=list)


def wrap_text(text, max_chars):
    """
    Breaks text into lines that fit. A single word longer than the limit is
    left alone rather than hyphenated — a place name is better slightly
    oversized than chopped in half.
    """
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fill_pattern(pattern, facts):
    """
    Interpolates {field} placeholders from catalog facts.

    Returns None when any placeholder has no value, so a caption is dropped
    rather than rendered with a hole in it. A missing place name should mean
    no label, never "Pizza in " or "{place_name}".
    """
    needed = set(re.findall(r"\{(\w+)\}", pattern))
    for name in needed:
        value = facts.get(name)
        if value is None or str(value).strip() == "":
            return None
    return re.sub(r"\{(\w+)\}", lambda m: str(facts[m.group(1)]).strip(), pattern)


def facts_for_clip(clip, asset, city_name=None):
    """Everything a caption pattern may draw on for one timeline entry."""
    return {
        "place_name": asset.get("place_name"),
        "category": asset.get("category"),
        "subcategory": asset.get("subcategory"),
        "subtype": asset.get("subtype"),
        "city": city_name,
        "role": clip.get("role"),
    }


def plan_captions(recipe, assets_by_pk, caption_specs, city_name=None,
                  overrides=None):
    """
    Turns template caption specs into timed captions against the recipe.

    Each spec names a slot role, a pattern, and a style; it resolves against
    whichever clip filled that role. A spec for a slot that was skipped, or
    whose clip lacks a needed fact, is recorded in `unresolved` rather than
    guessed at.
    """
    by_role = {}
    for clip in recipe.get("timeline", []):
        by_role.setdefault(clip["role"], clip)

    plan = CaptionPlan()
    for spec in caption_specs:
        role = spec.get("role")
        clip = by_role.get(role)
        if clip is None:
            plan.unresolved.append(f"{role}: slot not present in this render")
            continue

        asset = assets_by_pk.get(clip["asset_pk"], {})
        # An override is literal text a person wrote for this one video. It
        # skips pattern filling entirely, so it can say anything — which is
        # why it is only ever set by hand, never generated.
        override = (overrides or {}).get(role)
        if override is not None and str(override).strip():
            text = str(override).strip()
        else:
            text = fill_pattern(spec["pattern"],
                                facts_for_clip(clip, asset, city_name))
        if text is None:
            plan.unresolved.append(
                f"{role}: pattern {spec['pattern']!r} has no value for every field")
            continue

        length = clip["source_out_ms"] - clip["source_in_ms"]
        lead_in = int(spec.get("lead_in_ms", 200))
        hold = int(spec.get("hold_ms", 0)) or max(1200, length - lead_in - 200)
        start = clip["timeline_in_ms"] + lead_in
        end = min(start + hold, clip["timeline_in_ms"] + length)
        if end <= start:
            plan.unresolved.append(f"{role}: clip too short to hold a caption")
            continue

        plan.captions.append(Caption(text=text, start_ms=start, end_ms=end,
                                     style=spec.get("style", "label")))

    plan.captions.sort(key=lambda c: c.start_ms)
    return plan


def _widest_line_px(lines, font_px):
    return max((len(line) * font_px * CHAR_WIDTH_RATIO for line in lines),
               default=0.0)


def fit_caption(text, canvas, style):
    """
    Chooses a font size and line breaks so the text stays inside the frame.

    drawtext neither wraps nor shrinks on its own, so both are decided here.
    The usable width is the frame minus the side safe margins; the box border
    eats into it too, and it scales with the font, so it is part of the
    budget. Wrap to that width, and if the widest line still would not fit —
    a place name longer than the column, which wrap_text leaves whole rather
    than chop — step the size down until it does. Returns (font_px, lines).
    """
    usable = canvas["width"] * (1 - 2 * SAFE_SIDE)
    font_px = max(12, int(canvas["height"] * style["font_size_ratio"]))
    while True:
        border = int(font_px * 0.35)
        avail = max(1.0, usable - 2 * border)
        max_chars = max(4, int(avail / (font_px * CHAR_WIDTH_RATIO)))
        lines = wrap_text(text, max_chars)[: style["max_lines"]]
        if _widest_line_px(lines, font_px) + 2 * border <= usable or font_px <= 12:
            return font_px, lines
        font_px = max(12, int(font_px * 0.92))


def write_caption_files(captions, workdir, canvas):
    """
    Writes each caption's wrapped text to its own file.

    drawtext reads these with textfile=, which is what avoids escaping
    apostrophes and colons in place names.
    """
    written = []
    for index, caption in enumerate(captions):
        style = STYLES.get(caption.style, STYLES["label"])
        font_px, lines = fit_caption(caption.text, canvas, style)

        path = os.path.join(workdir, f"caption{index:02d}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        written.append({"caption": caption, "path": path,
                        "font_px": font_px, "lines": len(lines), "style": style})
    return written


def drawtext_filters(prepared, canvas, fontfile=None):
    """
    One drawtext clause per caption, chained.

    Y positions are computed inside the safe band so text clears platform
    UI. `enable` gates each clause to its own time window, which is how a
    single filter chain carries several captions.
    """
    safe_top = canvas["height"] * SAFE_TOP
    safe_bottom = canvas["height"] * SAFE_BOTTOM
    clauses = []

    for item in prepared:
        caption, style = item["caption"], item["style"]
        font_px, line_count = item["font_px"], item["lines"]
        block = font_px * 1.25 * line_count

        if style["position"] == "upper":
            y = safe_top + canvas["height"] * 0.06
        elif style["position"] == "center":
            y = (canvas["height"] - block) / 2
        else:
            y = safe_bottom - block

        # textfile= rather than text=: place names contain apostrophes, and
        # drawtext's escaping rules make those a source of silent breakage.
        clause = (
            f"drawtext=textfile='{item['path']}'"
            f":fontsize={font_px}"
            f":fontcolor=white"
            f":line_spacing={int(font_px * 0.25)}"
            f":box=1:boxcolor=black@{style['box_opacity']}"
            f":boxborderw={int(font_px * 0.35)}"
            f":x=(w-text_w)/2:y={int(y)}"
            f":enable='between(t,{caption.start_ms / 1000:.3f},"
            f"{caption.end_ms / 1000:.3f})'"
        )
        if fontfile:
            clause += f":fontfile='{fontfile}'"
        clauses.append(clause)

    return clauses


def to_srt(captions):
    """
    A portable sidecar. Platforms that accept an upload use it for
    accessibility and for viewers watching muted, which is most of them.
    """
    def stamp(ms):
        hours, ms = divmod(int(ms), 3_600_000)
        minutes, ms = divmod(ms, 60_000)
        seconds, ms = divmod(ms, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"

    blocks = []
    for index, caption in enumerate(captions, start=1):
        blocks.append(f"{index}\n{stamp(caption.start_ms)} --> "
                      f"{stamp(caption.end_ms)}\n{caption.text}\n")
    return "\n".join(blocks)


def find_font(candidates=None):
    """
    Locates a usable font file.

    drawtext needs a real file; relying on fontconfig fails on a minimal
    server image with an error that names the filter rather than the missing
    package. Install fonts-dejavu-core if none of these exist.
    """
    candidates = candidates or [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def plan_for_recipe(db, recipe, overrides=None):
    """
    Resolves a recipe's caption specs against the catalog.

    Shared by the render worker and by preview, so what the operator sees
    before rendering is what actually gets burned in — a preview that
    guessed differently would be worse than none.
    """
    specs = recipe.get("caption_specs") or []
    if not specs:
        return CaptionPlan()

    pks = [c["asset_pk"] for c in recipe.get("timeline", [])]
    if not pks:
        return CaptionPlan()

    rows = db.execute_query_as_dict("""
        SELECT a.id, a.place_name, a.category, a.subcategory, a.subtype,
               c.cityname
        FROM public.content_library_assets a
        LEFT JOIN public.cities_reference c ON c.cityid = a.cityid
        WHERE a.id = ANY(%(pks)s)
    """, {"pks": pks})
    rows = rows if isinstance(rows, list) else []
    city_name = next((r.get("cityname") for r in rows if r.get("cityname")), None)
    return plan_captions(recipe, {r["id"]: r for r in rows}, specs,
                         city_name=city_name, overrides=overrides)
