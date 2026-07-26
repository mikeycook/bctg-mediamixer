# Current S3 Inventory

## Source

The original summary was grounded in `clips_output.txt`. It was refreshed directly
from `s3://big-city-travel-guide-clips/ugc-assets/` on 2026-07-25 after reaction
clips were uploaded. Zero-byte folder-marker objects are treated as structural
placeholders, not media assets.

## Totals

| Measure | Value |
|---|---:|
| Non-empty media objects | 73 |
| Total bytes | 690,337,350 |
| Approximate size | 658.4 MiB |
| File format by extension | 42 `.mov`, 31 `.mp4` |
| App recordings | 6 |
| B-roll | 36 |
| Reaction objects | 31 |
| Unique reaction payloads | 17 |
| Unique media payloads overall | 59 |
| New York-associated assets | 40 |
| Tokyo-associated assets | 2 |
| Music/captions/exported objects | 0 |

The directory listing does not contain durations, codecs, dimensions, frame rates,
audio information, cryptographic checksums, or rights information. Those must be
measured or supplied during ingestion. The ETag-and-size comparison used in this
refresh is sufficient to flag likely byte-identical uploads but should be confirmed
with SHA-256 during ingestion.

## App recordings

| City | Feature folders represented | Files |
|---|---|---:|
| New York | `guide`, `livetracking`, `map` | 4 |
| Tokyo | `map`, `trips` | 2 |
| Barcelona, Boston, Chicago, Los Angeles, Paris, Philadelphia, Rome | Folder placeholders only | 0 |

Examples:

- `ugc-assets/app/new-york/guide/app_newyork_best_pizza.mov`
- `ugc-assets/app/new-york/livetracking/app_newyork_livetracking_detail.mov`
- `ugc-assets/app/tokyo/map/app_tokyo_map_photos.mov`

Observation: current app filenames use `newyork`, while directory names use `new-york`. The canonical city slug should be `new-york`; existing keys should not be renamed merely to correct this.

## B-roll by category

| Category | Files | Bytes | Approx. MiB |
|---|---:|---:|---:|
| Food | 31 | 146,895,211 | 140.1 |
| Hotels | 2 | 15,852,175 | 15.1 |
| Landmarks | 2 | 7,040,839 | 6.7 |
| Shopping | 1 | 3,528,234 | 3.4 |
| **Total** | **36** | **173,316,459** | **165.3** |

Food coverage is concentrated in New York:

| Food subcategory | Files |
|---|---:|
| Bagels | 4 |
| Bakery | 1 |
| Fancy | 12 |
| Pizza | 8 |
| Tacos | 6 |
| Burgers | 0 (placeholder only) |

The `fancy` directory is not a durable controlled category: it mixes venue/experience style with shot content. Preserve the folder, but classify those assets using structured fields such as `category=food`, `venue_style=upscale`, `shot_type=interior|dish|menu|sign`, and the actual place name.

## Reaction inventory

| Reaction folder | Objects |
|---|---:|
| `confused` | 1 |
| `excited` | 7 |
| `happy` | 2 |
| `shocked` | 8 |
| `surprised` | 13 |
| **Total** | **31** |

Reaction objects use `.mp4` and total 461,317,713 bytes (approximately 440.0 MiB).
Fourteen payloads appear twice under different emotion folders:

- 2 `happy/excited` clips also appear under `happy`;
- 5 `surprised/excited` clips also appear under `surprised`;
- 4 `shocked/surprised` clips also appear under `surprised`; and
- 3 `surprised/shocked` clips also appear under `surprised`.

This duplication is intentional: placing the same clip in two folders classifies
it as compatible with two reaction types. Preserve every object and folder
placement. At ingestion, assign a checksum to every object, establish a canonical
media identity for each unique payload, and union the folder-derived emotions as
tags on that identity. The selector must collapse candidates sharing the same
checksum so one performance is not weighted twice or selected twice in a render.

The current reaction filenames contain spaces, mixed capitalization, and repeated
underscore separators. Existing keys should remain unchanged; newly uploaded clips
should follow the canonical naming standard.

## Place clusters identifiable from filenames

The following names appear in keys and should be verified during review:

- Ess-a-Bagel
- Chococo
- Jazba
- Vintage Green
- L'Industrie
- Roberta's
- Los Tacos
- New York Palace
- Empire State Building
- Bucherer

Filenames are useful hints, not authoritative place identification.

## Empty but intentional prefixes

- `ugc-assets/music/`
- `ugc-assets/captions/`
- `ugc-assets/exported/`
- `ugc-assets/b-roll/sites/`

Reaction templates can be enabled only after the uploaded clips are probed,
rights-reviewed, tagged, and activated.

## Coverage risks

- City coverage is highly imbalanced: 95.2% of current assets are New York-associated.
- App feature coverage is sparse and inconsistent across cities.
- The current inventory cannot confirm vertical orientation or production-atom length.
- There is no explicit CTA asset class in the current tree.
- There is no rights/license metadata.
- Reaction objects intentionally appear in multiple emotion folders; ingestion
  must preserve all classifications while deduplicating the underlying media.
- Food terminology exists both in paths and filenames but is not normalized.
- Empty prefix markers should not become rows in `content_library_assets`.

## Immediate inventory actions

1. Probe all 73 objects with `ffprobe`.
2. Generate a SHA-256 or other stable content checksum during first download/processing.
3. Register all S3 objects without moving them.
4. Review place names and city IDs.
5. Add rights/source fields before any automated publishing.
6. Deduplicate reaction selection by checksum while retaining source-key lineage.
7. Review reaction emotion tags, audio, consent/releases, and usable trim windows.
