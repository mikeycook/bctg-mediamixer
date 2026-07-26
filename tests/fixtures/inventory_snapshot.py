"""
Reconstructs the 2026-07-25 S3 inventory in an in-process bucket.

The numbers here are the acceptance criteria from the design package, not
arbitrary test data: 73 non-empty media objects — 6 app, 36 b-roll, 31
reaction — resolving to 59 unique payloads, because 14 reaction objects are
byte-identical copies filed under a second emotion.

Also seeded: zero-byte folder markers (including one for ugc-assets/ itself,
which lists with an empty name), an object under ugc-assets/exported/, and a
non-media file. All three must be excluded from ingestion.
"""

APP_KEYS = [
    "ugc-assets/app/new-york/guide/app_newyork_best_pizza.mov",
    "ugc-assets/app/new-york/guide/app_newyork_guide_detail.mov",
    "ugc-assets/app/new-york/livetracking/app_newyork_livetracking_detail.mov",
    "ugc-assets/app/new-york/map/app_newyork_map_results.mov",
    "ugc-assets/app/tokyo/map/app_tokyo_map_photos.mov",
    "ugc-assets/app/tokyo/trips/app_tokyo_trips_detail.mov",
]

# food 31 (bagels 4, bakery 1, fancy 12, pizza 8, tacos 6), hotels 2,
# landmarks 2, shopping 1 = 36
BROLL_SPEC = [
    ("food/bagels/new-york", "ess-a-bagel", 4),
    ("food/bakery/new-york", "chococo", 1),
    ("food/fancy/new-york", "jazba", 12),
    ("food/pizza/new-york", "lindustrie", 8),
    ("food/tacos/new-york", "los-tacos", 6),
    ("hotels/new-york", "new-york-palace", 2),
    ("landmarks/new-york", "empire-state-building", 2),
    ("shopping/new-york", "bucherer", 1),
]

FOLDER_MARKERS = [
    "ugc-assets/",
    "ugc-assets/music/",
    "ugc-assets/captions/",
    "ugc-assets/exported/",
    "ugc-assets/b-roll/sites/",
    "ugc-assets/app/paris/",
    "ugc-assets/b-roll/food/burgers/",
]


def broll_keys():
    keys = []
    for folder, stem, count in BROLL_SPEC:
        for n in range(1, count + 1):
            keys.append(f"ugc-assets/b-roll/{folder}/{stem}_{n:03d}.mov")
    return keys


def reaction_objects():
    """
    Returns [(key, payload)] for 31 objects over 17 unique payloads.

    17 primaries: confused 1, excited 7, happy 2, shocked 7.
    14 aliases: shocked 1, surprised 13 — each byte-identical to a primary
    filed under a different emotion, which is how a clip is marked as
    suiting two reactions.

    Folder totals land at confused 1, excited 7, happy 2, shocked 8,
    surprised 13.
    """
    payloads = [f"reaction-payload-{i:02d}".encode() for i in range(1, 18)]
    objects = []

    primaries = (
        [("confused", payloads[0], 1)]
        + [("excited", p, i) for i, p in enumerate(payloads[1:8], start=1)]
        + [("happy", p, i) for i, p in enumerate(payloads[8:10], start=1)]
        + [("shocked", p, i) for i, p in enumerate(payloads[10:17], start=1)]
    )
    for emotion, payload, n in primaries:
        # Real reaction filenames carry spaces, capitals and doubled
        # underscores. The keys are never renamed.
        objects.append((f"ugc-assets/reactions/{emotion}/Reaction__{emotion.title()} {n:02d}.mp4",
                        payload))

    aliases = [("shocked", payloads[1], 90)]
    aliases += [("surprised", p, 90 + i) for i, p in enumerate(payloads[2:15], start=1)]
    for emotion, payload, n in aliases:
        objects.append((f"ugc-assets/reactions/{emotion}/Reaction__{emotion.title()} {n:02d}.mp4",
                        payload))

    return objects


def populate(s3_client, bucket):
    """Creates the bucket and every object. Returns expected counts."""
    s3_client.create_bucket(Bucket=bucket)

    for key in APP_KEYS:
        s3_client.put_object(Bucket=bucket, Key=key, Body=key.encode())
    for key in broll_keys():
        s3_client.put_object(Bucket=bucket, Key=key, Body=key.encode())
    for key, payload in reaction_objects():
        s3_client.put_object(Bucket=bucket, Key=key, Body=payload)

    for marker in FOLDER_MARKERS:
        s3_client.put_object(Bucket=bucket, Key=marker, Body=b"")

    # Must never be ingested as a source asset.
    s3_client.put_object(
        Bucket=bucket,
        Key="ugc-assets/exported/dev/2026/07/25/RND-01K123ABC456/final.mp4",
        Body=b"generated-media")
    s3_client.put_object(
        Bucket=bucket,
        Key="ugc-assets/exported/dev/2026/07/25/RND-01K123ABC456/manifest.json",
        Body=b"{}")
    # Non-media alongside sources.
    s3_client.put_object(Bucket=bucket, Key="ugc-assets/b-roll/notes.txt", Body=b"notes")

    return {
        "media": 73, "app": 6, "broll": 36, "reaction": 31,
        "unique_payloads": 59, "reaction_unique": 17, "reaction_aliases": 14,
        "markers": len(FOLDER_MARKERS), "exported": 2, "non_media": 1,
    }
