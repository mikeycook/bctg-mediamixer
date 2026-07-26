-- Initial rights classification. Run as the bigcity owner. Idempotent.
--
-- Rights are a settled fact here rather than a per-asset judgement, so they
-- are set in bulk instead of one row at a time in the review UI:
--
--   app, broll   self-produced        -> owned
--   reaction     purchased stock      -> licensed
--
-- Both are publishable; production selection allows owned and licensed.
-- The purchased reaction licence also covers the model releases for the
-- people appearing in those clips, which is what would otherwise make
-- consent a separate per-asset question.
--
-- This deliberately does NOT touch `status`. Rights and editorial review
-- are different gates: an asset can be fully rights-cleared and still have
-- the wrong place name. Assets reach 'active' one at a time, through
-- review.
--
-- Only rows still at 'unknown' are updated, so a later human correction is
-- never overwritten by a re-run.

BEGIN;

UPDATE public.content_library_assets
SET rights_status = 'owned',
    rights_source = 'self-produced'
WHERE asset_type IN ('app', 'broll')
  AND rights_status = 'unknown';

UPDATE public.content_library_assets
SET rights_status = 'licensed',
    rights_source = 'purchased stock licence'
WHERE asset_type = 'reaction'
  AND rights_status = 'unknown';

-- If the stock licence is term-limited rather than perpetual, set the
-- expiry so selection can refuse expired footage rather than discovering
-- the problem after publication:
--
--   UPDATE public.content_library_assets
--   SET rights_expires_at = 'YYYY-MM-DD'
--   WHERE asset_type = 'reaction';

COMMIT;

-- Expect: 42 owned (6 app + 36 broll), 31 licensed, 0 unknown.
SELECT rights_status, count(*) AS assets
FROM public.content_library_assets
GROUP BY rights_status ORDER BY 1;
