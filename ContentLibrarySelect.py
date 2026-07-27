"""
Brief -> eligible candidates -> ranked selection -> immutable recipe.

The recipe is the contract between selection and rendering: once written it
is not edited, and a revision is a new render with a new id.

Two properties matter more than the ranking quality.

Fail closed. Every hard filter is a reason to exclude, never to substitute.
A Tokyo brief with no Tokyo B-roll returns insufficient_assets; it does not
quietly reach for New York footage. Unreviewed, rights-unknown, missing and
wrong-orientation assets are ineligible, and being short of one slot fails
the whole brief rather than producing a shorter video nobody asked for.

Deterministic. The same brief and seed against the same catalog produce the
same recipe, so a render can be explained after the fact.

Selection is split so the interesting parts are pure: eligible_candidates
touches the database, everything after it is a function of the rows it
returned.
"""

import hashlib
import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Only these ever reach a render. Unknown, restricted and expired do not.
PUBLISHABLE_RIGHTS = ("owned", "licensed")

# Trimmed clips start slightly in: the first moments of a handheld shot are
# usually the least stable, and app recordings often open mid-gesture.
DEFAULT_LEAD_IN_MS = 250


class SelectionError(Exception):
    """Carries a machine-readable code so callers can branch on the reason."""

    def __init__(self, code: str, detail: str, diagnostics: Optional[Dict] = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.diagnostics = diagnostics or {}

    def as_dict(self):
        return {"error": self.code, "detail": self.detail,
                "diagnostics": self.diagnostics}


@dataclass(frozen=True)
class VideoBrief:  # noqa: D101
    cityid: Optional[str] = None
    city_slug: Optional[str] = None
    topic: Optional[str] = None
    template_id: str = "city-discovery-v1"
    target_duration_ms: int = 20000
    platforms: Tuple[str, ...] = ("instagram-reels",)
    environment: str = "dev"
    seed: Optional[str] = None
    allow_landscape: bool = False
    # Rotate through the library across renders. On by default, because a
    # second alternate shot of the same place exists precisely so successive
    # variations differ. Turn it off for a strictly reproducible edit.
    prefer_unused: bool = True
    # Literal caption text for this one video, keyed by slot role. Overrides
    # the template's pattern where set.
    caption_overrides: Dict[str, str] = field(default_factory=dict)
    # Emotion for reaction slots, matched against merged emotion tags:
    # surprised, excited, happy, shocked, confused.
    mood: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        return cls(
            cityid=data.get("cityid") or data.get("city_id"),
            city_slug=data.get("city_slug"),
            topic=data.get("topic"),
            template_id=data.get("template_id", "city-discovery-v1"),
            target_duration_ms=int(data.get("target_duration_ms", 20000)),
            platforms=tuple(data.get("platforms", ("instagram-reels",))),
            environment=data.get("environment", "dev"),
            seed=data.get("seed"),
            allow_landscape=bool(data.get("allow_landscape", False)),
            mood=(data.get("mood") or None),
            prefer_unused=bool(data.get("prefer_unused", True)),
            caption_overrides={k: str(v) for k, v in
                               (data.get("caption_overrides") or {}).items()
                               if str(v).strip()},
        )


@dataclass
class Slot:
    role: str
    asset_types: List[str]
    min_ms: int
    preferred_ms: int
    max_ms: int
    required: bool = True
    prefer_topic_match: bool = False
    city_agnostic_ok: bool = False
    # Shot kinds this slot wants, matched against subtype. A preference,
    # never a filter — it shapes the sequence when the footage allows and
    # gets out of the way when it does not.
    prefer_subtypes: List[str] = field(default_factory=list)
    # When true and the brief carries a mood, restrict this slot to assets
    # tagged with that emotion.
    match_mood: bool = False
    notes: str = ""


@dataclass
class Template:
    template_id: str
    version: int
    canvas: Dict[str, int]
    slots: List[Slot] = field(default_factory=list)
    # Text overlays, resolved against whichever clips fill the slots.
    captions: List[Dict[str, Any]] = field(default_factory=list)


def load_template(template_id, template_dir=TEMPLATE_DIR):
    path = Path(template_dir) / f"{template_id}.json"
    if not path.exists():
        raise SelectionError("recipe_invalid", f"unknown template: {template_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return Template(
        template_id=data["template_id"],
        version=int(data["version"]),
        canvas=data["canvas"],
        slots=[Slot(**{k: v for k, v in s.items() if k != "notes"},
                    notes=s.get("notes", "")) for s in data["slots"]],
        captions=data.get("captions", []),
    )


# ---------------------------------------------------------------------------
# Eligibility — the only part that touches the database
# ---------------------------------------------------------------------------

ELIGIBLE_SQL = """
SELECT id, asset_id, s3_key, s3_version_id, checksum_sha256, asset_type,
       category, subcategory, subtype, place_name, cityid, city_slug,
       city_agnostic, duration_ms, width, height, orientation, has_audio,
       frame_rate, quality_score, hook_compatibility, shot_type, last_seen_at,
       (SELECT count(*) FROM public.content_library_render_assets ra
        JOIN public.content_library_renders r ON r.id = ra.render_id
        WHERE ra.asset_id = public.content_library_assets.id
          AND r.state = 'succeeded') AS use_count,
       (SELECT array_agg(t.slug) FROM public.content_library_asset_tags at
        JOIN public.content_library_tags t ON t.id = at.tag_id
        WHERE at.asset_id = public.content_library_assets.id) AS tag_slugs
FROM public.content_library_assets
WHERE status = 'active'
  AND rights_status = ANY(%(rights)s)
  AND (rights_expires_at IS NULL OR rights_expires_at > now())
  AND duplicate_of_asset_id IS NULL          -- one performance, one weight
  AND missing_since IS NULL
  AND duration_ms IS NOT NULL
  AND checksum_sha256 IS NOT NULL            -- unprobed assets are not usable
  AND asset_type = ANY(%(types)s)
  AND duration_ms >= %(min_ms)s
"""


def eligible_candidates(db, brief: VideoBrief, slot: Slot):
    """
    Hard filters only. Anything that fails one is excluded, never downgraded.

    City matching happens here rather than in ranking: borrowing another
    city's footage is the single most damaging thing a selector could do
    quietly, so it is a filter, not a preference.
    """
    params = {
        "rights": list(PUBLISHABLE_RIGHTS),
        "types": list(slot.asset_types),
        "min_ms": slot.min_ms,
    }
    sql = ELIGIBLE_SQL

    if not slot.city_agnostic_ok and (brief.cityid or brief.city_slug):
        if brief.cityid:
            sql += " AND (cityid = %(cityid)s OR city_agnostic)"
            params["cityid"] = brief.cityid
        else:
            sql += " AND (city_slug = %(city_slug)s OR city_agnostic)"
            params["city_slug"] = brief.city_slug

    if slot.match_mood and brief.mood:
        # Emotion comes from the tag table, not from subtype: the sync
        # merges every folder-derived emotion onto the canonical row, so a
        # performance filed under two emotions is reachable by both. Reading
        # subtype would see only the folder its canonical key happened to
        # sit in.
        sql += """
          AND EXISTS (
              SELECT 1 FROM public.content_library_asset_tags at
              JOIN public.content_library_tags t ON t.id = at.tag_id
              WHERE at.asset_id = public.content_library_assets.id
                AND t.namespace = 'emotion' AND t.slug = %(mood)s)"""
        params["mood"] = brief.mood.strip().lower()

    rows = db.execute_query_as_dict(sql, params)
    rows = rows if isinstance(rows, list) else []

    if not brief.allow_landscape:
        # The canvas is 9:16. A landscape clip can only fill it by cropping
        # away most of the frame, so it is excluded rather than silently
        # letterboxed.
        rows = [r for r in rows if r.get("orientation") in (None, "portrait", "square")]
    return rows


# ---------------------------------------------------------------------------
# Ranking — pure
# ---------------------------------------------------------------------------

def shot_signal(row):
    """
    What kind of shot this is, for variety purposes.

    The catalog carries this in `subtype` — Interior, Exterior, Food — which
    is where it was actually entered during review. `shot_type` exists in
    the schema for the controlled vocabulary in `04-asset-standards.md` and
    takes precedence when populated, but reading `subtype` means the
    penalty works against the data as tagged rather than against the data
    the schema hoped for.
    """
    return ((row.get("shot_type") or row.get("subtype") or "") or "").strip().lower() or None


def score_candidate(row, brief: VideoBrief, slot: Slot, used_places, used_subcats,
                    used_shot_types=None):
    """
    Higher is better. Preferences only — anything disqualifying was already
    removed by eligible_candidates.
    """
    used_shot_types = used_shot_types or set()
    score = 0.0
    topic = (brief.topic or "").strip().lower()

    if topic and slot.prefer_topic_match:
        if (row.get("subcategory") or "").lower() == topic:
            score += 40
        elif (row.get("category") or "").lower() == topic:
            score += 25
        elif topic in (row.get("place_name") or "").lower():
            score += 20
        # Tags are brief-targetable too, which is what makes a descriptor
        # like `upscale` usable — venue style does not belong in the cuisine
        # field, but it is still something worth building a video around.
        elif topic in {(t or "").lower() for t in (row.get("tag_slugs") or [])}:
            score += 22
        if topic in " ".join(row.get("hook_compatibility") or []).lower():
            score += 10

    # Shot kind, so a template can describe a sequence rather than only a
    # set of durations: establish outside, go inside, then the payoff. Worth
    # less than a topic match, so a pizza brief never takes an off-topic
    # clip merely because it is the right kind of shot.
    shot = shot_signal(row)
    if slot.prefer_subtypes and shot:
        if shot in {s.strip().lower() for s in slot.prefer_subtypes}:
            score += 18

    # Duration closest to the slot's preference needs the least trimming.
    duration = row.get("duration_ms") or 0
    if duration:
        drift = abs(duration - slot.preferred_ms) / max(slot.preferred_ms, 1)
        score += max(0.0, 15 - drift * 15)

    quality = row.get("quality_score")
    if quality:
        score += float(quality) * 3

    # Rotation across renders. Two exterior shots of one restaurant are a
    # mistake inside a single video and an asset across a series of them —
    # this is what gives the second one its turn, so a run of variations
    # does not keep reaching for the same clip.
    #
    # It does mean selection depends on render history, so the same brief
    # can yield a different edit next week. The recipe still records exactly
    # what was chosen, which is the property that matters; set
    # prefer_unused=false for a strictly repeatable edit.
    if brief.prefer_unused:
        uses = row.get("use_count") or 0
        score += 12 if uses == 0 else -min(12, uses * 4)

    # Variety, penalised rather than forbidden — with a limited library,
    # refusing a repeat outright would fail briefs that could still produce
    # a decent video.
    #
    # Place and shot type are separate penalties because they describe
    # different mistakes. Two shots of the same restaurant is repetitive;
    # two *exteriors* of the same restaurant looks like an editing error.
    # Together they make that exact case the worst-scoring option, while an
    # exterior followed by a dish close-up of the same place stays viable —
    # which is what makes depth on one location useful rather than wasted.
    place = (row.get("place_name") or "").lower()
    if place and place in used_places:
        score -= 30
    shot = shot_signal(row)
    if shot and shot in used_shot_types:
        score -= 12
    subcat = (row.get("subcategory") or "").lower()
    if subcat and subcat in used_subcats:
        score -= 8

    if row.get("orientation") == "portrait":
        score += 5
    return score


def _rng(brief: VideoBrief):
    seed = brief.seed or f"{brief.template_id}|{brief.cityid or brief.city_slug}|{brief.topic}"
    return random.Random(hashlib.sha256(seed.encode()).hexdigest())


def choose_for_slot(candidates, brief, slot, used_ids, used_checksums,
                    used_places, used_subcats, rng, used_shot_types=None):
    """
    Best-scoring candidate not already committed to this render.

    Excluding used_checksums as well as used_ids is what stops the same
    footage appearing twice under two different keys — the reaction library
    has 14 such pairs.
    """
    pool = [c for c in candidates
            if c["id"] not in used_ids
            and (c.get("checksum_sha256") not in used_checksums)]
    if not pool:
        return None

    scored = [(score_candidate(c, brief, slot, used_places, used_subcats,
                              used_shot_types), c)
              for c in pool]
    best = max(s for s, _ in scored)
    # Ties broken by seeded choice, so a rerun of the same brief is stable
    # but the selector is not biased toward whatever the database returned
    # first.
    tied = [c for s, c in scored if s >= best - 0.001]
    return rng.choice(sorted(tied, key=lambda c: c["id"]))


# ---------------------------------------------------------------------------
# Timing and recipe assembly — pure
# ---------------------------------------------------------------------------

def fit_durations(slots: List[Slot], target_ms: int):
    """
    Distributes the brief's target duration across slots within their bounds.

    Starts at each slot's preference, then scales toward min or max. If the
    target cannot be reached even at the bounds, the achievable total is
    returned — validation reports the discrepancy rather than this function
    silently violating a slot's limits.
    """
    durations = [s.preferred_ms for s in slots]
    total = sum(durations)
    if total == target_ms or not slots:
        return durations

    grow = target_ms > total
    for _ in range(64):
        remaining = target_ms - sum(durations)
        if remaining == 0:
            break
        headroom = [
            (s.max_ms - d) if grow else (d - s.min_ms)
            for s, d in zip(slots, durations)
        ]
        available = sum(headroom)
        if available <= 0:
            break
        step = 0
        for i, room in enumerate(headroom):
            if room <= 0:
                continue
            share = int(round(remaining * (room / available)))
            share = max(-room, min(room, share)) if not grow else min(room, share)
            if grow:
                durations[i] += max(0, share)
            else:
                durations[i] -= max(0, min(room, -share if share < 0 else share))
            step += 1
        if step == 0:
            break
    return durations


def trim_window(asset, take_ms, lead_in_ms=DEFAULT_LEAD_IN_MS):
    """Where in the source clip to take `take_ms` from."""
    duration = asset.get("duration_ms") or 0
    take = min(take_ms, duration)
    start = min(lead_in_ms, max(0, duration - take))
    return int(start), int(start + take)


def build_recipe(brief, template, filled):
    """
    `filled` is a list of (slot, asset, take_ms) for the slots that were
    actually filled — not a list parallel to template.slots. An optional
    slot that found nothing is simply absent, and the timeline closes up
    behind it rather than leaving a gap.
    """
    timeline, timeline_at = [], 0
    for slot, asset, take_ms in filled:
        source_in, source_out = trim_window(asset, take_ms)
        timeline.append({
            "asset_id": asset["asset_id"],
            "asset_pk": asset["id"],
            "s3_key": asset["s3_key"],
            "s3_version_id": asset.get("s3_version_id"),
            "checksum_sha256": asset.get("checksum_sha256"),
            "role": slot.role,
            "source_in_ms": source_in,
            "source_out_ms": source_out,
            "timeline_in_ms": timeline_at,
            "transform": {"mode": "cover", "safe_crop": "center"},
            "audio_policy": {"mode": "keep" if asset.get("has_audio") else "silent"},
        })
        timeline_at += source_out - source_in

    return {
        "recipe_version": 1,
        "template": {"id": template.template_id, "version": template.version},
        "brief": {
            "cityid": brief.cityid, "city_slug": brief.city_slug,
            "topic": brief.topic, "mood": brief.mood,
            "platforms": list(brief.platforms),
            "target_duration_ms": brief.target_duration_ms,
            "environment": brief.environment, "seed": brief.seed,
            "caption_overrides": dict(brief.caption_overrides),
        },
        "canvas": template.canvas,
        "caption_specs": template.captions,
        "timeline": timeline,
        "total_duration_ms": timeline_at,
        "captions": [],
        "audio_mix": {"normalize_lufs": -14.0, "music": None, "voiceover": None},
        "renderer": {"name": "ffmpeg", "version": "record-at-runtime"},
    }


def select(db, brief: VideoBrief, template_dir=TEMPLATE_DIR):
    """
    Produces a recipe, or raises SelectionError with a code from the design
    package: insufficient_assets, rights_not_cleared, recipe_invalid.

    An optional slot that finds nothing is skipped and the video is built
    without it. Only a *required* slot going unfilled fails the brief —
    which is what lets a reaction beat be offered to every template without
    making reaction footage a precondition for making any video at all.
    """
    template = load_template(brief.template_id, template_dir)
    rng = _rng(brief)
    durations = fit_durations(template.slots, brief.target_duration_ms)

    filled = []
    used_ids, used_checksums = set(), set()
    used_places, used_subcats, used_shot_types = set(), set(), set()
    shortfall, skipped = {}, []

    for slot, take_ms in zip(template.slots, durations):
        candidates = eligible_candidates(db, brief, slot)
        # A slot needs a clip at least as long as its own minimum, not the
        # fitted duration, so a short brief cannot admit unusable footage.
        candidates = [c for c in candidates if (c.get("duration_ms") or 0) >= slot.min_ms]

        chosen = choose_for_slot(candidates, brief, slot, used_ids,
                                 used_checksums, used_places, used_subcats, rng,
                                 used_shot_types)
        if chosen is None:
            detail = {
                "asset_types": slot.asset_types,
                "min_ms": slot.min_ms,
                "eligible_before_dedupe": len(candidates),
            }
            if slot.match_mood and brief.mood:
                detail["mood"] = brief.mood
            if slot.required:
                shortfall[slot.role] = detail
            else:
                skipped.append(slot.role)
            continue

        used_ids.add(chosen["id"])
        if chosen.get("checksum_sha256"):
            used_checksums.add(chosen["checksum_sha256"])
        if chosen.get("place_name"):
            used_places.add(chosen["place_name"].lower())
        if chosen.get("subcategory"):
            used_subcats.add(chosen["subcategory"].lower())
        shot = shot_signal(chosen)
        if shot:
            used_shot_types.add(shot)
        filled.append((slot, chosen, take_ms))

    if shortfall:
        raise SelectionError(
            "insufficient_assets",
            f"no eligible asset for: {', '.join(sorted(shortfall))}",
            {"unfilled_slots": shortfall,
             "city": brief.cityid or brief.city_slug,
             "topic": brief.topic,
             "mood": brief.mood,
             "hint": "assets must be active, rights-cleared, probed, portrait, "
                     "and match the brief's city unless marked city_agnostic"},
        )

    recipe = build_recipe(brief, template, filled)
    if skipped:
        recipe["skipped_optional_slots"] = skipped
    return recipe
