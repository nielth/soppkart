"""When a species fruits, learned from the dates of its GBIF findings.

The season comes at different times in different places: summer species come
later further north and higher up, autumn species earlier. And it is not just
moved: in the south the season is longer. So each point of the season, the
day by which 5, 25, 50, 75 and 95 % of the findings have been made, is fitted
on its own against latitude and elevation (quantile regression). Written to
MODEL_DIR/<species>/season.json and used by now.py.
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import rasterio
from pyproj import Transformer
from sklearn.linear_model import QuantileRegressor

from soppkart import config, sources
from soppkart.features import FEATURE_KEYS, FEATURES_PATH

log = logging.getLogger(__name__)

# The points of the season that are fitted: share of the findings made by that day.
QUANTILES = {"q05": 0.05, "q25": 0.25, "q50": 0.5, "q75": 0.75, "q95": 0.95}
# Findings dated further than this from a first fit are taken as wrong dates (e.g. 1 January).
MAX_RESIDUAL_DAYS = 75
# Before the first and after the last 5 % of the findings, the season fades out over this many days.
TAIL_DAYS = 14
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
    """Latitude and elevation as the fit sees them: degrees from 60°N and hundreds of metres."""
    return np.column_stack([lat - 60, elevation_m / 100])


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
    # A first, ordinary fit to drop findings with wrong dates.
    A = np.column_stack([np.ones(len(X)), X])
    keep = np.abs(doy - A @ np.linalg.lstsq(A, doy, rcond=None)[0]) <= MAX_RESIDUAL_DAYS
    X, doy = X[keep], doy[keep]
    fits = {}
    for name, q in QUANTILES.items():
        model = QuantileRegressor(quantile=q, alpha=0, solver="highs").fit(X, doy)
        fits[name] = [round(float(model.intercept_), 3), *(round(float(c), 3) for c in model.coef_)]
    season = {
        "species": species.key,
        "n_findings": int(keep.sum()),
        # Day of year = c0 + c1 * (lat - 60) + c2 * elevation / 100, per point of the season.
        "fits": fits,
        # The fits are only trusted within the places the findings come from.
        "lat_range": [round(float(v), 2) for v in np.percentile(lat[keep], [1, 99])],
        "elevation_range_m": [round(float(v)) for v in np.percentile(elev[keep], [1, 99])],
    }
    path = season_path(species)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(season))
    tmp.replace(path)
    for where, la, el in (("60°N, 100 m", 60, 100), ("69.6°N, 50 m", 69.6, 50)):
        days = season_days(season, la, el)
        log.info(
            "%s season at %s: %s",
            species.key,
            where,
            ", ".join(f"{k} {day_text(v)}" for k, v in days.items()),
        )
    return season


def day_text(doy: float) -> str:
    return (date(2025, 1, 1) + timedelta(days=round(doy) - 1)).strftime("%d.%m")


def load_season(species: config.Species) -> dict[str, Any] | None:
    """The species' season, or None if not learned yet (or learned by an older version)."""
    try:
        data = dict(json.loads(season_path(species).read_text()))
    except (OSError, ValueError):
        return None
    return data if "fits" in data else None


def season_days(season: dict[str, Any], lat: float, elevation_m: float) -> dict[str, float]:
    """Day of year of each point of the season (q05..q95) at a place."""
    lat = float(np.clip(lat, *season["lat_range"]))
    elevation_m = float(np.clip(elevation_m, *season["elevation_range_m"]))
    x = design(np.array([lat]), np.array([elevation_m]))[0]
    days = [c[0] + float(np.dot(c[1:], x)) for c in season["fits"].values()]
    # Separate fits can cross far from the data; keep them in order.
    return dict(zip(season["fits"], np.maximum.accumulate(days).tolist(), strict=True))


def season_factor(days: dict[str, float], doy: float) -> float:
    """How far into the season a day is (0..1).

    1 in the middle half of the season (q25..q75): the findings peak on the
    busiest weekends, but the mushroom is out the whole time. From there down to
    0.3 at q05 and q95, and to 0 over TAIL_DAYS beyond them.
    """
    q05, q25, q75, q95 = days["q05"], days["q25"], days["q75"], days["q95"]
    if q25 <= doy <= q75:
        return 1.0
    if q05 <= doy < q25:
        return 0.3 + 0.7 * (doy - q05) / max(q25 - q05, 1)
    if q75 < doy <= q95:
        return 0.3 + 0.7 * (q95 - doy) / max(q95 - q75, 1)
    outside = q05 - doy if doy < q05 else doy - q95
    return 0.3 * max(0.0, 1 - outside / TAIL_DAYS)
