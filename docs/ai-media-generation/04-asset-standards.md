# Asset Naming and Tagging Standards

## General rule

Paths help humans navigate; PostgreSQL metadata drives automation. Existing keys remain valid even when they do not match the new convention.

## Canonical slugs

- lowercase ASCII;
- words separated by hyphens;
- no spaces or underscores in controlled slugs;
- stable once published.

Examples: `new-york`, `los-angeles`, `live-tracking`, `hidden-gem`.

## New filenames

Use:

```text
{place-or-subject}_{shot-type}_{qualifier}_{sequence}.{ext}
```

Examples:

```text
lindustrie_pizza-closeup_open-box_001.mov
empire-state-building_wide-dusk_001.mov
app_new-york_map_pizza-results_001.mov
reaction_surprised_point-right_001.mov
```

Rules:

- lowercase;
- underscore separates major semantic blocks;
- hyphen joins words inside a block;
- three-digit sequence;
- no dates unless the event/date is editorially significant;
- no adjective such as `best` as an objective metadata claim;
- do not embed every tag in the filename;
- never reuse a key for different content.

## Asset IDs

Keep human-readable IDs independent of S3 keys:

- source asset: `UGC-00001` (existing convention);
- render: sortable `RND-{ULID}`;
- template: stable slug plus version, e.g. `city-discovery-v1`.

Sequence allocation must happen in PostgreSQL or a single service, not by scanning S3.

## Controlled `asset_type`

- `app`
- `broll`
- `reaction`
- `music`
- `voiceover`
- `caption`
- `cta`

Generated media belongs in render tables, not as a source asset unless explicitly re-imported.

## Controlled semantic fields

### App feature

Examples: `guide`, `map`, `live-tracking`, `trips`, `search`, `favorites`, `offline`, `place-detail`.

### B-roll category

Examples: `food`, `hotel`, `landmark`, `shopping`, `street`, `transport`, `museum`, `nightlife`.

### Shot type

Examples: `exterior`, `interior`, `wide`, `medium`, `close-up`, `detail`, `food-prep`, `approach`, `sign`, `menu`, `screen-flow`.

### Camera motion

`static`, `pan-left`, `pan-right`, `tilt-up`, `tilt-down`, `push-in`, `pull-out`, `tracking`, `handheld`, `unknown`.

### Reaction emotion/action

Examples: `surprised`, `excited`, `amused`, `skeptical`, `curious`, `pointing`, `thinking`, `approval`.

## Tags

Tags are supplemental descriptors, not substitutes for typed fields. A tag record has:

- canonical slug;
- display label;
- tag namespace;
- optional aliases;
- active/inactive state.

Recommended namespaces:

- `theme`: `hidden-gem`, `luxury`, `budget`, `iconic`
- `mood`: `energetic`, `calm`, `romantic`
- `visual`: `neon`, `crowded`, `rain`, `sunset`
- `audience`: `first-time-visitor`, `foodie`, `family`
- `compatibility`: `hook`, `transition`, `cta-background`

Do not store full marketing sentences as tags.

## Hook compatibility

Represent hooks as stable concepts with separately editable copy:

```text
hook key: hidden-gem
display copy: "Nobody tells tourists about this place."
```

An asset may be compatible with a hook concept, with provenance and confidence. This replaces uncontrolled phrases inside `hook_compatibility TEXT[]`.

## Required metadata before activation

Every production-eligible video asset must have:

- stable `asset_id`;
- S3 bucket/key and last-seen state;
- measured duration, width, height, frame rate, and codecs;
- orientation;
- asset type;
- rights status and source;
- review status;
- city or explicit `city_agnostic=true`;
- no corruption/probe error.

App and B-roll assets additionally require category/feature classification. Reaction assets require emotion/action and consent/release status when a recognizable person appears.

## Quality guidelines

Preferred source production atoms:

- reaction: 3–6 seconds;
- app action: 4–8 seconds;
- B-roll detail: 3–6 seconds;
- establishing B-roll: 5–8 seconds.

Longer masters may be retained, but mark them for atomization. Avoid baked-in copyrighted music. Preserve original audio; record whether speech or music is present.

## Existing exceptions

- `app_newyork_*` remains valid though `new-york` is canonical.
- `b-roll/food/fancy/` remains valid physically but maps to normalized metadata.
- Existing reaction filenames with spaces, capitalization, and repeated underscores
  remain valid S3 keys but receive normalized database names and tags.
- Identical reaction payloads in multiple emotion folders are intentional
  classification aliases. Every folder contributes an emotion tag, while the
  payload remains one performance for weighting and selection.
- Misspellings in existing filenames are handled with database corrections and aliases, not destructive S3 renames.
