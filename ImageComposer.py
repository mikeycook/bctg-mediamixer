"""
Compose still images from a template and a set of photos + text.

The still-image counterpart to VideoRenderer: a template names portrait
(1080x1920) image slots and text slots with absolute canvas coordinates; this
places each chosen photo into its slot (center-crop to cover, or contain with
letterboxing) and draws each text band over the top. Pure Pillow — no ffmpeg.

The compose() function is pure: it takes already-loaded PIL images and text
strings and returns a finished RGB image. S3 I/O (fetching the source photos,
uploading the result) lives in the API layer, so this stays unit-testable
without a network or a bucket.
"""
import json
import os
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    # Importing this module must never take the service down. If Pillow is not
    # installed, template listing and validation still work; only compose()
    # fails, with a clear message, until `pip install Pillow` is run.
    Image = ImageDraw = ImageFont = None

import CaptionBuilder as _captions  # reuse the font locator

TEMPLATE_DIR = Path(__file__).resolve().parent / "image_templates"

_BOLD_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]
_REGULAR_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


class ImageComposeError(Exception):
    """A template that cannot be loaded, or a compose that cannot be built."""


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def load_image_template(template_id, template_dir=TEMPLATE_DIR):
    path = Path(template_dir) / f"{template_id}.json"
    if not path.exists():
        raise ImageComposeError(f"unknown image template: {template_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_template(data)
    return data


def validate_template(t):
    canvas = t.get("canvas") or {}
    width, height = canvas.get("width"), canvas.get("height")
    if not width or not height:
        raise ImageComposeError("template canvas needs width and height")
    seen = set()
    for slot in list(t.get("image_slots") or []) + list(t.get("text_slots") or []):
        name = slot.get("name")
        if not name:
            raise ImageComposeError("every slot needs a name")
        if name in seen:
            raise ImageComposeError(f"duplicate slot name: {name}")
        seen.add(name)
        for k in ("x", "y", "w", "h"):
            if not isinstance(slot.get(k), int):
                raise ImageComposeError(f"slot {name} needs integer {k}")
        if (slot["x"] < 0 or slot["y"] < 0
                or slot["x"] + slot["w"] > width
                or slot["y"] + slot["h"] > height):
            raise ImageComposeError(f"slot {name} falls outside the canvas")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _rgb(value, default=(0, 0, 0)):
    if not value:
        return default
    v = str(value).lstrip("#")
    if len(v) < 6:
        return default
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def _font(size, bold=True):
    candidates = _BOLD_FONTS if bold else _REGULAR_FONTS
    path = _captions.find_font(candidates)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _cover(img, w, h):
    """Scale to cover w×h, then center-crop — fills the slot, no distortion."""
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    nw, nh = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _contain(img, w, h, bg):
    """Scale to fit inside w×h, centered on a `bg` field — nothing is cropped."""
    src_w, src_h = img.size
    scale = min(w / src_w, h / src_h)
    nw, nh = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    field = Image.new("RGB", (w, h), bg)
    field.paste(img, ((w - nw) // 2, (h - nh) // 2))
    return field


def _line_width(font, text):
    try:
        return font.getlength(text)
    except AttributeError:
        return font.getsize(text)[0]


def _wrap(font, text, max_width, max_lines):
    """Greedy word-wrap to max_width, capped at max_lines with an ellipsis."""
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if _line_width(font, trial) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    # Anything left over is truncated onto the last line with an ellipsis.
    consumed = sum(len(l.split()) for l in lines)
    if consumed < len(words) and lines:
        last = lines[-1]
        while _line_width(font, last + "…") > max_width and " " in last:
            last = last.rsplit(" ", 1)[0]
        lines[-1] = last + "…"
    return lines or [""]


def _fit_text(text, box_w, box_h, base_size, bold, max_lines, padding):
    """Shrink the font until the wrapped text fits the padded band."""
    avail_w = max(1, box_w - 2 * padding)
    avail_h = max(1, box_h - 2 * padding)
    size = max(12, base_size)
    while size >= 12:
        font = _font(size, bold)
        lines = _wrap(font, text, avail_w, max_lines)
        line_h = int(size * 1.25)
        if line_h * len(lines) <= avail_h:
            return font, lines, line_h
        size -= 4
    font = _font(12, bold)
    return font, _wrap(font, text, avail_w, max_lines), int(12 * 1.25)


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------
def compose(template, images, texts):
    """
    Build the finished image.

    images: {slot_name: PIL.Image} for image slots that were filled.
    texts:  {slot_name: str}        for text slots that were filled.
    Returns an RGB PIL.Image sized to the template canvas.
    """
    if Image is None:
        raise ImageComposeError("Pillow is not installed on this server")
    canvas_cfg = template["canvas"]
    W, H = canvas_cfg["width"], canvas_cfg["height"]
    bg = _rgb(canvas_cfg.get("background"), (0, 0, 0))
    canvas = Image.new("RGBA", (W, H), bg + (255,))

    for slot in template.get("image_slots") or []:
        src = images.get(slot["name"])
        if src is None:
            if slot.get("required"):
                raise ImageComposeError(f"image slot '{slot['name']}' has no photo")
            continue
        src = src.convert("RGB")
        w, h = slot["w"], slot["h"]
        placed = (_contain(src, w, h, bg) if slot.get("fit") == "contain"
                  else _cover(src, w, h))
        canvas.paste(placed.convert("RGBA"), (slot["x"], slot["y"]))

    draw = ImageDraw.Draw(canvas, "RGBA")
    for slot in template.get("text_slots") or []:
        text = (texts.get(slot["name"]) or "").strip()
        if not text:
            if slot.get("required"):
                raise ImageComposeError(f"text slot '{slot['name']}' has no text")
            continue
        x, y, w, h = slot["x"], slot["y"], slot["w"], slot["h"]

        band = slot.get("background")
        if band:
            opacity = float(slot.get("background_opacity", 1.0))
            alpha = max(0, min(255, round(opacity * 255)))
            draw.rectangle([x, y, x + w, y + h], fill=_rgb(band) + (alpha,))

        padding = int(slot.get("padding", 24))
        font, lines, line_h = _fit_text(
            text, w, h, int(slot.get("font_size", 72)), bool(slot.get("bold", True)),
            int(slot.get("max_lines", 2)), padding)
        color = _rgb(slot.get("color"), (255, 255, 255)) + (255,)

        total_h = line_h * len(lines)
        valign = slot.get("valign", "middle")
        if valign == "top":
            cursor_y = y + padding
        elif valign == "bottom":
            cursor_y = y + h - padding - total_h
        else:
            cursor_y = y + (h - total_h) // 2

        align = slot.get("align", "center")
        for line in lines:
            lw = _line_width(font, line)
            if align == "left":
                lx = x + padding
            elif align == "right":
                lx = x + w - padding - lw
            else:
                lx = x + (w - lw) // 2
            draw.text((lx, cursor_y), line, font=font, fill=color)
            cursor_y += line_h

    return canvas.convert("RGB")


def render_to_file(template, images, texts, out_path, quality=90):
    img = compose(template, images, texts)
    img.save(out_path, format="JPEG", quality=quality)
    return img.size  # (width, height)
