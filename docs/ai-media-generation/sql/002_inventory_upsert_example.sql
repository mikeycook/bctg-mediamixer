-- Parameterized example for one discovered object.
-- Keep assignment of the human-facing UGC asset_id in application code or a dedicated DB function.

INSERT INTO public.content_library_assets (
    asset_id,
    bucket_name,
    s3_key,
    filename,
    folder,
    size_bytes,
    content_type,
    etag,
    s3_version_id,
    s3_last_modified_at,
    asset_type,
    type,
    cityid,
    category,
    subcategory,
    status,
    first_seen_at,
    last_seen_at,
    missing_since
) VALUES (
    :asset_id,
    :bucket_name,
    :s3_key,
    :filename,
    :folder,
    :size_bytes,
    :content_type,
    :etag,
    :s3_version_id,
    :s3_last_modified_at,
    :asset_type,
    :legacy_type,
    :cityid,
    :category,
    :subcategory,
    'discovered',
    :inventory_started_at,
    :inventory_started_at,
    NULL
)
ON CONFLICT (bucket_name, s3_key)
DO UPDATE SET
    filename = EXCLUDED.filename,
    folder = EXCLUDED.folder,
    size_bytes = EXCLUDED.size_bytes,
    content_type = EXCLUDED.content_type,
    etag = EXCLUDED.etag,
    s3_version_id = EXCLUDED.s3_version_id,
    s3_last_modified_at = EXCLUDED.s3_last_modified_at,
    last_seen_at = EXCLUDED.last_seen_at,
    missing_since = NULL,
    status = CASE
        WHEN public.content_library_assets.status = 'missing' THEN 'discovered'
        ELSE public.content_library_assets.status
    END;

-- Only run missing reconciliation after a complete, successful bucket listing.
UPDATE public.content_library_assets
SET
    status = 'missing',
    missing_since = COALESCE(missing_since, :inventory_completed_at)
WHERE bucket_name = :bucket_name
  AND s3_key LIKE 'ugc-assets/%'
  AND s3_key NOT LIKE 'ugc-assets/exported/%'
  AND last_seen_at < :inventory_started_at
  AND status NOT IN ('archived', 'rejected', 'missing');

