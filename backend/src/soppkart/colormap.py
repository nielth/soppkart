"""Colour ramp for the score tiles. Keep in sync with frontend/src/lib/Legend.svelte."""

from functools import lru_cache

import numpy as np

# Colours along the visible part of the score range: 0 = the threshold,
# 1 = the best score. Everything below the threshold is transparent.
RAMP = [
    (0.0, "#ffffb2"),
    (0.3, "#fecc5c"),
    (0.6, "#fd8d3c"),
    (0.8, "#f03b20"),
    (1.0, "#bd0026"),
]
ALPHA = 220
# Show the best 5 % by default.
DEFAULT_THRESHOLD = 0.95


def _hex(color: str) -> tuple[int, int, int]:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


@lru_cache(maxsize=128)
def colormap(threshold: float = DEFAULT_THRESHOLD) -> dict[int, tuple[int, int, int, int]]:
    """Map raster values (1..255 = score percentile, 0 = no data) to RGBA.

    Scores below threshold (0..1) are transparent; the ramp spans threshold..1.
    """
    positions = [p for p, _ in RAMP]
    channels = np.array([_hex(c) for _, c in RAMP], dtype=np.float64).T
    cmap: dict[int, tuple[int, int, int, int]] = {0: (0, 0, 0, 0)}
    span = max(1.0 - threshold, 1e-6)
    for value in range(1, 256):
        score = (value - 1) / 254
        if score < threshold:
            cmap[value] = (0, 0, 0, 0)
            continue
        rel = (score - threshold) / span
        r, g, b = (round(float(np.interp(rel, positions, ch))) for ch in channels)
        cmap[value] = (r, g, b, ALPHA)
    return cmap
