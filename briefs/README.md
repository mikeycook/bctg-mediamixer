# Briefs

A brief is the only thing you write to request a video. It says *what kind
of video*, not *which clips* — selection decides that from the catalog,
under rules that fail closed rather than substituting something plausible.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `cityid` | yes* | The `CIT-` identifier. Assets from other cities are excluded outright, never borrowed. |
| `city_slug` | yes* | Alternative to `cityid`, e.g. `new-york`. Use one or the other. |
| `topic` | no | Matched against `subcategory` first, then `category`, then place name and hooks. A *preference*: a thin topic still produces a video, drawing on the next best thing. |
| `template_id` | no | Default `city-discovery-v1`. Defines the slots, their durations, and the shot progression. |
| `target_duration_ms` | no | Default 20000. Distributed across slots within their individual limits, so the result lands near this rather than exactly on it. |
| `platforms` | no | Recorded on the render for later attribution. Does not change the output today — everything renders 1080×1920. |
| `seed` | no | Any string. The same brief and seed reproduce the same recipe. Omit and one is derived from the brief, which is still stable for that brief. |
| `allow_landscape` | no | Default false. Landscape clips are excluded because filling a 9:16 frame from one means cropping most of it away. |

\* One of `cityid` or `city_slug`.

## Running one

```bash
# See what would be chosen. Renders nothing, writes nothing.
python3 RenderWorker.py --brief-file briefs/new-york-pizza.json --dry-run

# Actually make it.
python3 RenderWorker.py --brief-file briefs/new-york-pizza.json

# Or inline, for a one-off.
python3 RenderWorker.py --brief '{"cityid":"CIT-00000000002","topic":"tacos"}'
```

Always dry-run first. It costs a second and shows exactly which clips,
which trim windows, and how long the result will be.

## What a brief cannot do

It cannot name specific clips. That is deliberate: selection is
rule-driven and reproducible, so a video can be explained after the fact
from its recipe rather than from whoever assembled it.

If you want a particular clip in a particular video, the levers are the
catalog and the template — tag the clip so it ranks well, or write a
template whose slots ask for what you want. If that turns out to be
routine rather than occasional, a `pin` field on the brief would be worth
adding; it does not exist yet because nothing has needed it.

## Failure is informative

A brief that cannot be filled returns `insufficient_assets` and names
every unfilled slot, with what it was looking for. That is the intended
behaviour, not an error to work around — see
`tokyo-ramen-expected-failure.json`.
