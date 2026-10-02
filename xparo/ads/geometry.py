"""Where an ad goes on the screen.

Each placement owns a region of the screen (a side column, a top/bottom
strip, the middle, or everything). The ad is scaled to fit inside its
region *keeping its own aspect ratio* -- a 16:9 video in the left column
becomes a 16:9 box as wide as the column, not a stretched one -- and sits
against the screen edge its placement names."""

# (x, y, width, height) as fractions of the screen; y grows downwards.
REGIONS = {
    "fullscreen": (0.0, 0.0, 1.0, 1.0),
    "left": (0.0, 0.0, 0.30, 1.0),
    "right": (0.70, 0.0, 0.30, 1.0),
    "top": (0.0, 0.0, 1.0, 0.22),
    "bottom": (0.0, 0.78, 1.0, 0.22),
    "center": (0.20, 0.20, 0.60, 0.60),
}
AUDIO_CARD = (440, 120)  # a voice ad shows a small "now playing" card


def region(screen_w, screen_h, placement):
    fx, fy, fw, fh = REGIONS.get(placement, REGIONS["center"])
    return fx * screen_w, fy * screen_h, fw * screen_w, fh * screen_h


def ad_box(screen_w, screen_h, placement, media_size=None, media_type="image"):
    """(x, y, width, height) in whole pixels for an ad on a screen_w x
    screen_h screen. media_size: the file's own (width, height), when
    known; without it the ad fills its region (text) or gets a small card
    (audio)."""
    rx, ry, rw, rh = region(screen_w, screen_h, placement)
    if media_type == "audio":
        w, h = min(AUDIO_CARD[0], rw), min(AUDIO_CARD[1], rh)
    elif media_size and media_size[0] > 0 and media_size[1] > 0:
        scale = min(rw / media_size[0], rh / media_size[1])
        w, h = media_size[0] * scale, media_size[1] * scale
    else:
        w, h = rw, rh
    # anchored to the edge the placement names, centred along it
    if placement == "left":
        x, y = rx, ry + (rh - h) / 2
    elif placement == "right":
        x, y = rx + rw - w, ry + (rh - h) / 2
    elif placement == "top":
        x, y = rx + (rw - w) / 2, ry
    elif placement == "bottom":
        x, y = rx + (rw - w) / 2, ry + rh - h
    else:
        x, y = rx + (rw - w) / 2, ry + (rh - h) / 2
    return int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))
