"""
Music credits for a finished video.

A track's licence may require attribution — CC BY does — and these Reels
promote a paid product, so an omitted credit is a licence breach, not a
discourtesy. What a track owes is decided from the track's own fields,
recorded in content_library_music_tracks, never guessed: cc0 and public
domain owe nothing; the -nc classes should not be here at all.

Two outputs. A sidecar, attribution.txt, carrying the full credit for the
publisher to paste into the post description — the authoritative version. And
a short burn-in line for the end of the video, for the majority of viewers
who never open a description. The sidecar is the one that satisfies the
licence; the burn-in is a courtesy that also happens to look intentional.

The tracks come from the recipe under key "music": a list of dicts shaped
like content_library_music_tracks rows. Absent that key — no music selected —
every function here returns empty and the render is unchanged.
"""

MUSIC_NOTE = "♪"  # ♪


def credit_line(track):
    """
    One track's full credit.

    Prefers the reviewer-written attribution_text (that is exactly what the
    field is for); otherwise composes TASL — Title, Author, Source, Licence —
    from whatever fields are present.
    """
    explicit = (track.get("attribution_text") or "").strip()
    if explicit:
        return explicit

    title = (track.get("title") or "").strip()
    artist = (track.get("artist") or "").strip()
    parts = []
    if title and artist:
        parts.append(f"{title} by {artist}")
    elif title or artist:
        parts.append(title or artist)

    source = (track.get("source_url") or track.get("source") or "").strip()
    if source:
        parts.append(source)
    licence = (track.get("license_url") or track.get("license") or "").strip()
    if licence:
        parts.append(licence)
    return " | ".join(parts)


def tracks_needing_credit(tracks):
    """
    The credit lines a set of tracks owes.

    Only tracks flagged attribution_required, and only where a line actually
    resolves — a required credit with no usable field is dropped here and
    surfaced by callers rather than emitting a blank.
    """
    lines = []
    for track in tracks or []:
        if not track.get("attribution_required"):
            continue
        line = credit_line(track)
        if line:
            lines.append(line)
    return lines


def attribution_text(tracks, header="Music:"):
    """The sidecar body, or '' when nothing needs crediting."""
    lines = tracks_needing_credit(tracks)
    if not lines:
        return ""
    return header + "\n" + "\n".join(lines) + "\n"


def burn_in_credit(tracks):
    """
    The short on-screen line for the outro, or '' when nothing needs it.

    One track gets a real credit; several get a pointer to the description,
    because a wall of credits burned into the frame helps no one and the
    sidecar carries them all in full anyway.
    """
    crediting = [t for t in (tracks or []) if t.get("attribution_required")
                 and credit_line(t)]
    if not crediting:
        return ""
    if len(crediting) > 1:
        return f"{MUSIC_NOTE} Music credits in description"

    track = crediting[0]
    title = (track.get("title") or "").strip()
    artist = (track.get("artist") or "").strip()
    if title and artist:
        return f"{MUSIC_NOTE} {title} — {artist}"
    if title or artist:
        return f"{MUSIC_NOTE} {title or artist}"
    return f"{MUSIC_NOTE} Music in description"
