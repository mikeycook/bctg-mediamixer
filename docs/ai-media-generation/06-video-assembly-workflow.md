# Video Assembly Workflow

## Input: a structured brief

Example:

```json
{
  "city_id": "new-york",
  "topic": "pizza",
  "platforms": ["instagram-reels", "tiktok"],
  "target_duration_ms": 20000,
  "template_id": "city-discovery-v1",
  "tone": "energetic",
  "include_reaction": false,
  "include_app": true,
  "rights_policy": "production"
}
```

The brief must distinguish required constraints from preferences.

## Template slots

Recommended reaction-free starter template:

| Slot | Target | Required | Eligible types |
|---|---:|---|---|
| Hook visual | 2–3 s | yes | B-roll |
| Destination proof | 4–6 s | yes | B-roll |
| App demonstration | 5–8 s | yes | App |
| Supporting visual | 3–5 s | yes | B-roll |
| CTA | 2–3 s | yes | App/B-roll background plus generated text |

Reaction template, activated after uploads:

| Slot | Target | Required | Eligible types |
|---|---:|---|---|
| Reaction/hook | 3–5 s | yes | Reaction |
| Destination proof | 4–6 s | yes | B-roll |
| App demonstration | 5–8 s | yes | App |
| CTA | 2–3 s | yes | App/B-roll background |

## Candidate eligibility

Hard filters:

- `status = active`;
- object is not missing;
- rights are `owned` or valid `licensed`;
- suitable media dimensions/orientation;
- asset type matches slot;
- city matches brief unless `city_agnostic`;
- duration/usable trim can fill the slot;
- no incompatible baked-in music or speech policy;
- no same source asset repeated unless the template permits it.
- no two source rows with the same content checksum in one render.

Soft ranking:

- category/subcategory match;
- place/topic match;
- hook compatibility;
- preferred duration;
- quality score;
- unused or least-recently-used;
- varied place and shot types;
- prior performance, only after sufficient unbiased data exists.

## Current-library behavior

- A New York pizza template has multiple B-roll candidates and several matching New York app recordings.
- Tokyo has app recordings but no Tokyo B-roll in the supplied inventory, so a Tokyo destination-specific recipe should return `insufficient_assets`, not borrow New York footage.
- Reaction-required templates can now be planned after at least one compatible
  reaction is probed, rights-cleared, reviewed, and activated.
- The 31 reaction keys represent 17 unique performances with intentional
  multi-emotion classification. A performance is eligible through any merged
  emotion tag, but its alias keys must not add ranking weight or appear together in
  a render.

## Recipe

Selection produces an immutable recipe before rendering:

```json
{
  "recipe_version": 1,
  "canvas": {"width": 1080, "height": 1920, "fps": 30},
  "timeline": [
    {
      "asset_id": "UGC-00001",
      "role": "hook_visual",
      "source_in_ms": 500,
      "source_out_ms": 3500,
      "timeline_in_ms": 0,
      "transform": {"mode": "cover", "safe_crop": "center"}
    }
  ],
  "captions": [],
  "audio_mix": {},
  "renderer": {"name": "ffmpeg", "version": "record-at-runtime"}
}
```

The recipe is the contract between selection and rendering.

## Text generation

Generate hook, narration, caption, and CTA copy from verified catalog facts. Do not introduce unverified superlatives such as “best,” Michelin status, wait times, or awards.

Store:

- prompt/template version;
- model name/version;
- generated copy;
- moderation/validation outcome;
- approved final copy.

## Audio policy

For each source:

1. preserve the original source object;
2. inspect for speech/music;
3. choose keep, attenuate, or mute;
4. mix optional voiceover/music during render;
5. duck ambience under voiceover;
6. normalize final loudness to a configured social profile.

Music requires an explicit license record. No licensed track means use voiceover/ambience or a cleared library—not arbitrary platform audio baked into the exported master.

## Rendering

The worker:

1. claims a queued render;
2. resolves exact S3 object versions;
3. downloads and verifies sources;
4. applies trims, scaling/cropping, transitions, text, captions, and audio mix;
5. creates delivery and preview artifacts;
6. runs technical validation;
7. uploads to the render's immutable exported directory;
8. writes artifact rows and manifest;
9. marks the render succeeded or failed.

## Validation

Require:

- playable H.264/AAC MP4;
- 1080×1920, 30 fps;
- exact/allowed duration tolerance;
- no black/frozen frames beyond configured thresholds;
- audio peak/loudness within policy;
- captions inside safe zones;
- all source asset/version references resolvable;
- manifest checksum matches each artifact.

## Failure modes

Return explicit errors:

- `insufficient_assets`
- `rights_not_cleared`
- `source_missing`
- `probe_incompatible`
- `recipe_invalid`
- `render_failed`
- `validation_failed`
- `export_failed`

Never substitute a wrong-city or rights-unknown asset just to complete a render.
