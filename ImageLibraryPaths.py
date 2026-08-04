"""
Classify source-photo keys under ugc-assets/images/.

Layout you upload into (parallel to the video clip folders, but its own tree
so the video sync never touches it):

    ugc-assets/images/<city-slug>/<topic>/<file>.jpg

city-slug resolves to a city the same way the clip library does; topic lands
in `subcategory` (e.g. burgers), which is what image templates scope to.
"""
from dataclasses import dataclass
from typing import Optional

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


def classify(key: str, prefix: str = IMAGES_PREFIX) -> ClassifiedImage:
    filename = key.rsplit("/", 1)[-1]
    folder = key[:len(key) - len(filename)].rstrip("/")
    if not key.startswith(prefix):
        return ClassifiedImage(key=key, filename=filename, folder=folder)
    rest = key[len(prefix):]
    segments = [s for s in rest.split("/") if s]
    # segments = [city-slug, topic, ..., filename]
    city_slug = segments[0] if len(segments) >= 2 else None
    subcategory = segments[1] if len(segments) >= 3 else None
    return ClassifiedImage(
        key=key, filename=filename, folder=folder,
        city_slug=city_slug, subcategory=subcategory,
        recognized=bool(city_slug),
    )
