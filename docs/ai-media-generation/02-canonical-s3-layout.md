# Canonical S3 Layout

## Compatibility rule

The current S3 structure is accepted as the source-of-truth physical layout for existing objects. Do not perform a bulk rename or move. Canonicalization happens in PostgreSQL through normalized asset types, city slugs, categories, tags, and aliases.

## Current-compatible layout

```text
s3://big-city-travel-guide-clips/
└── ugc-assets/
    ├── app/
    │   └── {city-slug}/{feature}/{existing-filename}.mov
    ├── b-roll/
    │   ├── food/{subcategory}/{city-slug}/{existing-filename}.mov
    │   ├── hotels/{city-slug}/{existing-filename}.mov
    │   ├── landmarks/{city-slug}/{existing-filename}.mov
    │   ├── shopping/{city-slug}/{existing-filename}.mov
    │   └── sites/{city-slug}/{existing-filename}.mov
    ├── reactions/
    │   └── {emotion-or-action}/{filename}.mov
    ├── music/
    ├── captions/
    └── exported/
```

Adding the optional subfolder below `reactions/` does not invalidate existing reaction objects if they are initially placed directly in the folder.

## Logical asset types

Folder paths map to normalized database values:

| Physical prefix | `asset_type` |
|---|---|
| `ugc-assets/app/` | `app` |
| `ugc-assets/b-roll/` | `broll` |
| `ugc-assets/reactions/` | `reaction` |
| `ugc-assets/music/` | `music` |
| `ugc-assets/captions/` | `caption` |
| `ugc-assets/exported/` | not a source asset by default; tracked as a render artifact |

## New source-object recommendations

Continue the existing pattern for new app and B-roll assets. Use canonical hyphenated city slugs:

```text
ugc-assets/app/{city-slug}/{feature}/{filename}.mov
ugc-assets/b-roll/food/{food-subcategory}/{city-slug}/{filename}.mov
ugc-assets/b-roll/{category}/{city-slug}/{filename}.mov
```

Do not introduce a second, parallel “perfect” source hierarchy. That would fragment discovery. The database absorbs semantic inconsistencies while the source hierarchy evolves conservatively.

## Generated-media layout

All generated media must remain beneath `ugc-assets/exported/`:

```text
ugc-assets/exported/
└── {environment}/
    └── {yyyy}/{mm}/{dd}/
        └── {render-id}/
            ├── final.mp4
            ├── preview.mp4
            ├── thumbnail.jpg
            ├── captions.srt
            ├── manifest.json
            └── validation.json
```

Allowed environments: `dev`, `staging`, `prod`.

Example:

```text
ugc-assets/exported/prod/2026/07/25/RND-01K123ABC456/final.mp4
```

The render directory is immutable. A new attempt or revision gets a new render ID. `final.mp4` is a delivery artifact, not a mutable “latest” pointer.

## Non-published working material

Intermediate downloads, normalized mezzanine files, and frame caches should use ephemeral worker storage. If persistent cache is later needed, create a distinct prefix such as `ugc-assets/_processing-cache/` with lifecycle expiration. Do not mix cache files with source assets or `exported/`.

## S3 controls

- Enable bucket versioning.
- Block public access; distribute through controlled application/CDN mechanisms.
- Encrypt objects at rest.
- Add lifecycle rules for previews/cache only, never for masters without explicit approval.
- Use object tags sparingly for operational facts such as `environment`, `render_id`, and `artifact_role`.
- Keep rich semantic metadata in PostgreSQL.
- Record S3 `etag`, `version_id`, `last_modified`, and size during inventory sync.

