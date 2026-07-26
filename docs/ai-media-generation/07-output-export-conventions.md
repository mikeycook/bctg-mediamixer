# Output and Export Conventions

## Required destination

Every generated media artifact must be written below:

```text
s3://big-city-travel-guide-clips/ugc-assets/exported/
```

No generated media may be written into `app/`, `b-roll/`, `reactions/`, or other source prefixes.

## Render directory

```text
ugc-assets/exported/{environment}/{yyyy}/{mm}/{dd}/{render-id}/
```

Standard artifacts:

| Name | Required | Purpose |
|---|---|---|
| `final.mp4` | yes | Delivery-quality vertical master |
| `manifest.json` | yes | Recipe, lineage, versions, checksums |
| `validation.json` | yes | Automated QA results |
| `preview.mp4` | recommended | Smaller review copy |
| `thumbnail.jpg` | recommended | Review/poster image |
| `captions.srt` | when captions exist | Portable caption track |

Platform-specific derivatives may be added as:

```text
tiktok.mp4
instagram-reels.mp4
youtube-shorts.mp4
```

Do not overwrite an existing artifact. Revisions receive a new render ID.

## Render ID

Use `RND-` plus an uppercase ULID:

```text
RND-01K123ABC456...
```

This provides sortable, globally safe IDs without leaking marketing copy or user data into keys.

## Manifest minimum

```json
{
  "schema_version": 1,
  "render_id": "RND-...",
  "environment": "prod",
  "created_at": "2026-07-25T12:00:00Z",
  "brief": {},
  "template": {"id": "city-discovery-v1", "version": 1},
  "recipe": {},
  "sources": [
    {
      "asset_id": "UGC-00001",
      "bucket": "big-city-travel-guide-clips",
      "s3_key": "ugc-assets/...",
      "s3_version_id": null,
      "checksum_sha256": "..."
    }
  ],
  "tools": {
    "renderer": "ffmpeg",
    "renderer_version": "...",
    "copy_model": "...",
    "prompt_version": "..."
  },
  "artifacts": [
    {
      "role": "final",
      "s3_key": "ugc-assets/exported/.../final.mp4",
      "checksum_sha256": "..."
    }
  ]
}
```

## Database state

Recommended render states:

`draft`, `planned`, `queued`, `rendering`, `validating`, `succeeded`, `failed`, `cancelled`.

Mark `succeeded` only after:

- final artifact upload succeeds;
- validation passes;
- manifest uploads successfully; and
- artifact database rows commit.

## Local development output

Local render output should mirror the artifact names inside a temporary render-specific directory. The upload step is responsible for writing to S3. Local files are disposable after checksums and uploads are confirmed.

For this technical package itself, the desktop environment had no `/mnt/data` mount, so the documents were generated in the project-local `exported/` directory. This does not change the S3 rule for generated media.

## Retention

- Production finals and manifests: retain.
- Failed render logs: retain for a bounded diagnostic window.
- Preview artifacts: lifecycle according to review needs.
- Temporary worker cache: delete automatically after successful upload or expiry.
- Never apply generated-output lifecycle rules to source masters.

