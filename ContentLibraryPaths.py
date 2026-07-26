"""
Path classification for the UGC clip library.

Every function here is pure: no S3, no database, no filesystem. Values
derived from a path are proposals with provenance 'path', never 'human' —
existing keys predate the naming standard and a folder name is a hint, not
an authority. Review corrects them.

Existing S3 keys are never renamed to match the standard. Canonicalization
happens in PostgreSQL.
"""

import re
from dataclasses import dataclass, field
from typing import Optional, Tuple

DEFAULT_PREFIX = "ugc-assets/"
EXPORTED_PREFIX = "ugc-assets/exported/"

# Mirrors the admin backend's _CLIP_MEDIA_EXT so both agree on what counts
# as media. Images are included because the backend accepts them, though the
# current library is entirely .mov and .mp4.
MEDIA_EXTENSIONS = (
    ".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv",
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".webp",
)

ASSET_TYPE_BY_PREFIX = {
    "app": "app",
    "b-roll": "broll",
    "broll": "broll",
    "reactions": "reaction",
    "reaction": "reaction",
    "music": "music",
    "captions": "caption",
    "caption": "caption",
    "cta": "cta",
}

# Folder names are plural; the controlled vocabulary is singular.
CATEGORY_ALIASES = {
    "hotels": "hotel",
    "landmarks": "landmark",
    "sites": "site",
    "restaurants": "restaurant",
    "museums": "museum",
}


@dataclass(frozen=True)
class ClassifiedPath:
    key: str
    filename: str
    folder: str
    asset_type: Optional[str] = None
    city_slug: Optional[str] = None
    feature: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    emotions: Tuple[str, ...] = field(default_factory=tuple)
    recognized: bool = False


def slugify(value):
    """
    Lowercase, hyphen-separated, alphanumerics only.

    This must stay byte-identical to the expression in
    migration/ExportFromServer2.sh that derives cities_reference.city_slug
    from cityname. City resolution joins a slug produced here against one
    produced there, so any divergence silently fails to resolve rather than
    erroring — hence the shared test.
    """
    if value is None:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    return slug or None


def is_folder_marker(key, size):
    """
    Zero-byte objects that exist only to make a prefix visible in the
    console. The bucket has several, including one for ugc-assets/ itself,
    which lists with an empty name. They are structure, not assets.
    """
    return key.endswith("/") or not size


def is_exported(key):
    """Generated media. Never ingested as a source asset."""
    return key.startswith(EXPORTED_PREFIX)


def is_media(key):
    return key.lower().endswith(MEDIA_EXTENSIONS)


def classify(key, prefix=DEFAULT_PREFIX):
    """
    Derives asset type, city, and classification from an object key.

    Layouts recognized, per the canonical S3 layout:

        app/{city}/{feature}/{file}
        b-roll/food/{subcategory}/{city}/{file}
        b-roll/{category}/{city}/{file}
        reactions/{emotion}/{file}
        reactions/{emotion}/{emotion}/{file}

    A reaction key contributes every folder segment as an emotion. That is
    intentional rather than lenient: the same performance is filed under
    two emotions to mean it suits both, and those labels are merged onto
    one canonical asset later.
    """
    filename = key.rsplit("/", 1)[-1]
    folder = (key.rsplit("/", 1)[0] + "/") if "/" in key else ""

    rest = key[len(prefix):] if key.startswith(prefix) else key
    parts = [p for p in rest.split("/") if p]
    if not parts:
        return ClassifiedPath(key=key, filename=filename, folder=folder)

    asset_type = ASSET_TYPE_BY_PREFIX.get(parts[0].lower())
    if asset_type is None:
        return ClassifiedPath(key=key, filename=filename, folder=folder)

    segments = [slugify(s) for s in parts[1:-1]]

    if asset_type == "app":
        return ClassifiedPath(
            key=key, filename=filename, folder=folder, asset_type=asset_type,
            city_slug=segments[0] if len(segments) > 0 else None,
            feature=segments[1] if len(segments) > 1 else None,
            recognized=True,
        )

    if asset_type == "broll":
        category = subcategory = city = None
        if len(segments) >= 3:
            category, subcategory, city = segments[0], segments[1], segments[2]
        elif len(segments) == 2:
            # Positional, so b-roll/food/pizza/ with clips directly inside
            # would read "pizza" as a city. Deliberately not special-cased:
            # the sync reports cityid values that do not resolve against
            # cities_reference, which catches it without inventing rules
            # that may not hold.
            category, city = segments[0], segments[1]
        elif len(segments) == 1:
            category = segments[0]
        return ClassifiedPath(
            key=key, filename=filename, folder=folder, asset_type=asset_type,
            category=CATEGORY_ALIASES.get(category, category),
            subcategory=subcategory, city_slug=city, recognized=True,
        )

    if asset_type == "reaction":
        emotions = tuple(dict.fromkeys(s for s in segments if s))
        return ClassifiedPath(
            key=key, filename=filename, folder=folder, asset_type=asset_type,
            emotions=emotions, recognized=True,
        )

    return ClassifiedPath(
        key=key, filename=filename, folder=folder,
        asset_type=asset_type, recognized=True,
    )
