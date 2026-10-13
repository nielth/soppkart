"""When a species fruits, learned from the dates of its GBIF findings.

The season comes later further north and higher up, so the day of the year of
each finding is fitted against latitude and elevation. What is left over (the
finding's day minus the fitted day for its place) is how early or late in the
season it was; their spread is the shape of the season, the same everywhere
but centred on the fitted day for the place. Written to
MODEL_DIR/<species>/season.json and used by now.py.
"""

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import rasterio
from pyproj import Transformer

from soppkart import config, sources
from soppkart.features import FEATURE_KEYS, FEATURES_PATH

log = logging.getLogger(__name__)

# Days before and after the fitted peak that the season shape covers.
SPAN_DAYS = 120
# Smoothing of the season shape (days).
BANDWIDTH_DAYS = 6
# Findings dated further than this from the fit are taken as wrong dates (e.g. 1 January).
MAX_RESIDUAL_DAYS = 75
# Older findings say less about today's climate.
MIN_YEAR = 1990
MIN_FINDINGS = 30


def season_path(species: config.Species) -> Path:
    return config.MODEL_DIR / species.key / "season.json"


def dated_findings(species: config.Species) -> pl.DataFrame:
    """Findings with a single day as date, with day of year; refetches old downloads without dates."""
    path = sources.findings_path(species)
    if not path.exists() or "month" not in pl.read_parquet_schema(path):
        sources.fetch_findings(species, refresh=True)
    df = pl.read_parquet(path).filter(
        pl.col("month").is_not_null()
        & pl.col("day").is_not_null()
        & (pl.col("year") >= MIN_YEAR)
        # Placeholder dates in old records.
        & ~((pl.col("month") == 1) & (pl.col("day") == 1))
    )
    doy = [
        date(y, m, d).timetuple().tm_yday
        for y, m, d in df.select("year", "month", "day").iter_rows()
    ]
    return df.with_columns(doy=pl.Series(doy, dtype=pl.Float64))


def elevations(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Elevation (m) of each spot from the features raster; NaN outside the land."""
    x, y = Transformer.from_crs("EPSG:4326", config.CRS, always_xy=True).transform(lon, lat)
    band = FEATURE_KEYS.index("elevation") + 1
    with rasterio.open(FEATURES_PATH) as src:
        values = np.array([v[0] for v in src.sample(zip(x, y, strict=True), indexes=band)])
        if src.nodata is not None:
            values = np.where(values == src.nodata, np.nan, values)
    return values.astype(np.float64)


def design(lat: np.ndarray, elevation_m: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones_like(lat), lat - 60, elevation_m / 100])


def build_season(species: config.Species) -> dict[str, Any] | None:
    """Fit and write the species' season; None if there are too few dated findings."""
    df = dated_findings(species)
    lat, lon, doy = (df[c].to_numpy() for c in ("lat", "lon", "doy"))
    elev = elevations(lat, lon)
    ok = np.isfinite(elev)
    lat, doy, elev = lat[ok], doy[ok], elev[ok]
    if len(doy) < MIN_FINDINGS:
        log.warning("%s: only %d dated findings, no season", species.key, len(doy))
        return None
    X = design(lat, elev)
    coef = np.linalg.lstsq(X, doy, rcond=None)[0]
    keep = np.abs(doy - X @ coef) <= MAX_RESIDUAL_DAYS
    coef = np.linalg.lstsq(X[keep], doy[keep], rcond=None)[0]
    residual = (doy - X @ coef)[keep]

    offsets = np.arange(-SPAN_DAYS, SPAN_DAYS + 1)
    density = np.exp(-0.5 * ((offsets[:, None] - residual[None, :]) / BANDWIDTH_DAYS) ** 2).sum(1)
    density /= density.max()
    q05, q25, q75, q95 = np.percentile(residual, [5, 25, 75, 95])
    season = {
        "species": species.key,
        "n_findings": int(keep.sum()),
        # Fitted peak day of year = c0 + c1 * (lat - 60) + c2 * elevation / 100.
        "coef": [round(float(c), 4) for c in coef],
        "quantiles": {
            k: round(float(v), 1)
            for k, v in zip(("q05", "q25", "q75", "q95"), (q05, q25, q75, q95), strict=True)
        },
        "span_days": SPAN_DAYS,
        "density": [round(float(d), 4) for d in density],
    }
    path = season_path(species)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(season))
    tmp.replace(path)
    log.info(
        "%s season from %d findings: peak day %.0f at 60°N/0 m, +%.1f days per degree north, "
        "+%.1f per 100 m",
        species.key,
        season["n_findings"],
        *coef,
    )
    return season


def load_season(species: config.Species) -> dict[str, Any] | None:
    try:
        return dict(json.loads(season_path(species).read_text()))
    except (OSError, ValueError):
        return None


def peak_day(season: dict[str, Any], lat: float, elevation_m: float) -> float:
    """Fitted peak day of year at a place."""
    c0, c1, c2 = season["coef"]
    return float(c0 + c1 * (lat - 60) + c2 * elevation_m / 100)


def density_at(season: dict[str, Any], offset_days: float) -> float:
    """Findings at offset_days from the fitted day, relative to the busiest day (0..1)."""
    span = season["span_days"]
    i = round(offset_days) + span
    return float(season["density"][i]) if 0 <= i < len(season["density"]) else 0.0


def season_factor(season: dict[str, Any], offset_days: float) -> float:
    """How far into the season offset_days is: 1 in the middle half, falling off outside it.

    The findings peak sharply on the busiest weekends, but the mushroom is out
    the whole middle half of the season (q25..q75), so that counts in full;
    outside it the factor follows the findings, scaled to meet 1 at the edge.
    """
    q = season["quantiles"]
    if q["q25"] <= offset_days <= q["q75"]:
        return 1.0
    edge = density_at(season, q["q25"] if offset_days < q["q25"] else q["q75"])
    return min(1.0, density_at(season, offset_days) / max(edge, 1e-6))
