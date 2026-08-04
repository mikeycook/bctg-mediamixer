"""
Classify source-photo keys under ugc-assets/images/.

A photo's key carries a city and a topic in its folders:

    ugc-assets/images/<topic>/<city-slug>/<file>.jpg     (or city-slug/topic — either order)

Order-independent: when the set of known city slugs is supplied (the sync
passes it from cities_reference), whichever folder segment is a known slug is
the city and the other is the topic. Without that set — pure calls, tests —
it falls back to positional <topic>/<city-slug>. topic lands in `subcategory`
(e.g. burgers), which is what image templates scope to.
"""
from dataclasses import dataclass
from typing import Iterable, Optional

import ContentLibraryPaths as _clp

IMAGES_PREFIX = "ugc-assets/images/"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

slugify = _clp.slugify
is_folder_marker = _clp.is_folder_marker


def is_image(key: str) -> bool:
    return key.lower().endswith(IMAGE_EXTENSIONS)


@dataclass(frozen=True)
class ClassifiedImage:
    key: str
    filename: str
    folder: str
    city_slug: Optional[str] = None
    subcategory: Optional[str] = None
    recognized: bool = False


def classify(key: str, prefix: str = IMAGES_PREFIX,
             known_city_slugs: Optional[Iterable[str]] = None) -> ClassifiedImage:
    filename = key.rsplit("/", 1)[-1]
    folder = key[:len(key) - len(filename)].rstrip("/")
    if not key.startswith(prefix):
        return ClassifiedImage(key=key, filename=filename, folder=folder)

    # Folder segments only (drop the filename).
    segments = [s for s in key[len(prefix):].split("/")[:-1] if s]
    city_slug = subcategory = None

    known = {s.lower() for s in (known_city_slugs or [])}
    if known:
        city_slug = next((s for s in segments if s.lower() in known), None)
        subcategory = next((s for s in segments if s != city_slug), None)

    # Positional fallback (no known set, or the city wasn't recognised):
    # the layout is <topic>/<city-slug>.
    if city_slug is None and subcategory is None:
        if len(segments) >= 1:
            subcategory = segments[0]
        if len(segments) >= 2:
            city_slug = segments[1]

    return ClassifiedImage(
        key=key, filename=filename, folder=folder,
        city_slug=city_slug, subcategory=subcategory,
        recognized=bool(city_slug),
    )
