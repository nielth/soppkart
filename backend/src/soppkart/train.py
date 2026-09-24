"""Train an XGBoost model per species and write its score raster.

The model only sees nature features (forest, terrain, soil, water, land cover,
rain). Registered findings are used only as training labels: cells with a
finding are contrasted with a random sample of land cells ("background").
Findings are presence-only, so scores are relative: the output raster holds
each cell's percentile rank among all land cells.
"""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import numpy as np
import polars as pl
import rasterio
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.windows import Window
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import GroupKFold
from xgboost import XGBClassifier

from soppkart import config, sources, userfindings
from soppkart.features import (
    FEATURE_KEYS,
    FEATURES,
    FEATURES_PATH,
    LAND_PATH,
    load_features_meta,
    raster_profile,
)
from soppkart.grid import Grid

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelFiles:
    """Output files for one species, in MODEL_DIR/<species key>/."""

    dir: Path

    @property
    def score(self) -> Path:
        return self.dir / "score.tif"

    @property
    def meta(self) -> Path:
        return self.dir / "meta.json"

    @property
    def model(self) -> Path:
        return self.dir / "model.json"

    @property
    def findings(self) -> Path:
        return self.dir / "findings.geojson"


def model_files(species_key: str) -> ModelFiles:
    return ModelFiles(config.MODEL_DIR / species_key)


BACKGROUND_RATIO = 10
BLOCK_SIZE_M = 50_000
SEED = 42


def load_findings(grid: Grid, species: config.Species) -> pl.DataFrame:
    """GBIF and own findings with grid row/col and a 'source' column ("gbif" or "own").

    Findings outside the grid are dropped.
    """
    gbif = pl.read_parquet(sources.findings_path(species)).select(
        "lat", "lon", "year", pl.col("uncertainty_m").cast(pl.Float64), source=pl.lit("gbif")
    )
    own = userfindings.as_frame(species.key).with_columns(source=pl.lit("own"))
    df = pl.concat([gbif, own])
    to_grid = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    x, y = to_grid.transform(df["lon"].to_numpy(), df["lat"].to_numpy())
    row, col = grid.rowcol(np.asarray(x), np.asarray(y))
    return df.with_columns(row=row, col=col).filter(pl.col("row") >= 0)


def make_model() -> XGBClassifier:
    return XGBClassifier(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        tree_method="hist",
        enable_categorical=True,
        feature_types=["c" if f.categorical else "q" for f in FEATURES],
        eval_metric="auc",
        random_state=SEED,
        n_jobs=-1,
    )


FOREST_IDX = [FEATURE_KEYS.index(k) for k in ("pct_lauv", "pct_furu", "pct_gran", "pct_bland")]
CULTIVATED_IDX = [FEATURE_KEYS.index(k) for k in ("pct_aker", "pct_slatt")]


def habitat(species: config.Species, feats: np.ndarray) -> np.ndarray:
    """True where the species' habitat rules allow it (feats: [band, ...])."""
    forest = feats[FOREST_IDX].sum(axis=0)
    ok = np.ones(forest.shape, dtype=bool)
    if species.min_forest_pct is not None:
        ok &= forest >= species.min_forest_pct
    if species.max_forest_pct is not None:
        ok &= forest <= species.max_forest_pct
    if species.max_cultivated_pct is not None:
        ok &= feats[CULTIVATED_IDX].sum(axis=0) <= species.max_cultivated_pct
    return ok


def habitat_text(species: config.Species) -> str | None:
    """The habitat rules in words, for the map."""
    parts = []
    if species.min_forest_pct is not None:
        parts.append(f"minst {species.min_forest_pct:g} % skog")
    if species.max_forest_pct is not None:
        parts.append(f"maks {species.max_forest_pct:g} % skog")
    if species.max_cultivated_pct is not None:
        parts.append(f"maks {species.max_cultivated_pct:g} % åker/slått eng")
    return ", ".join(parts) or None


def collect_training_data(
    species: config.Species, positives: pl.DataFrame, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Feature rows for presence cells and a random sample of other habitat cells.

    Only land cells inside the species' habitat are used. Reads features.tif one
    land chunk at a time. Returns (X, y, rows, cols).
    """
    meta = load_features_meta()
    n_pos = positives.height
    # The habitat is only part of the land, so sample generously and trim below.
    p_background = min(1.0, 3 * BACKGROUND_RATIO * n_pos / meta["n_land_cells"])
    pos_rows = positives["row"].to_numpy()
    pos_cols = positives["col"].to_numpy()
    xs, ys, rows_out, cols_out = [], [], [], []
    with rasterio.open(FEATURES_PATH) as fsrc, rasterio.open(LAND_PATH) as lsrc:
        assert list(fsrc.descriptions) == FEATURE_KEYS, "features.tif is outdated, rebuild features"
        for row0, col0, height, width in meta["land_chunks"]:
            window = Window(col0, row0, width, height)  # type: ignore[call-arg]
            feats = fsrc.read(window=window)
            land = lsrc.read(1, window=window).astype(bool) & habitat(species, feats)
            inside = (
                (pos_rows >= row0)
                & (pos_rows < row0 + height)
                & (pos_cols >= col0)
                & (pos_cols < col0 + width)
            )
            pr, pc = pos_rows[inside] - row0, pos_cols[inside] - col0
            # Findings in towns, sea or outside the habitat are not used.
            on_land = land[pr, pc]
            pr, pc = pr[on_land], pc[on_land]
            candidates = land.copy()
            candidates[pr, pc] = False
            br, bc = np.nonzero(candidates)
            pick = rng.random(len(br)) < p_background
            br, bc = br[pick], bc[pick]
            for r, c, label in ((pr, pc, 1.0), (br, bc, 0.0)):
                xs.append(feats[:, r, c].T)
                ys.append(np.full(len(r), label))
                rows_out.append(r + row0)
                cols_out.append(c + col0)
    X, y = np.concatenate(xs), np.concatenate(ys)
    rows, cols = np.concatenate(rows_out), np.concatenate(cols_out)
    background = np.flatnonzero(y == 0)
    n_background = min(len(background), BACKGROUND_RATIO * int(y.sum()))
    keep = np.concatenate(
        [np.flatnonzero(y == 1), rng.choice(background, n_background, replace=False)]
    )
    return X[keep], y[keep], rows[keep], cols[keep]


def train(species: config.Species) -> None:
    grid = Grid.norway()
    files = model_files(species.key)
    files.dir.mkdir(parents=True, exist_ok=True)
    log.info("training %s", species.name)

    findings = load_findings(grid, species)
    precise = findings.filter(
        pl.col("uncertainty_m").is_not_null()
        & (pl.col("uncertainty_m") <= config.FINDINGS_MAX_UNCERTAINTY_M)
    )
    positives = precise.select("row", "col").unique()
    n_own = int((precise["source"] == "own").sum())
    log.info(
        "%d findings, %d located within %d m (%d of them your own), in %d cells",
        findings.height,
        precise.height,
        config.FINDINGS_MAX_UNCERTAINTY_M,
        n_own,
        positives.height,
    )

    rng = np.random.default_rng(SEED)
    X, y, rows, cols = collect_training_data(species, positives, rng)
    n_pos, n_bg = int(y.sum()), int((y == 0).sum())
    log.info("training on %d presence and %d background cells", n_pos, n_bg)

    # Cells with one of your own findings weigh more.
    own_cells = set(precise.filter(pl.col("source") == "own").select("row", "col").iter_rows())
    is_own = np.array([(r, c) in own_cells for r, c in zip(rows, cols, strict=True)]) & (y == 1)
    weight = np.where(is_own, config.OWN_FINDING_WEIGHT, 1.0)

    # Spatial cross-validation: whole ~50 km blocks are held out together, so
    # the score reflects how well the model does in areas it hasn't seen.
    block = BLOCK_SIZE_M // grid.res
    groups = (rows // block) * 100_000 + cols // block
    oof = np.zeros(len(y))
    for fold, (tr, te) in enumerate(GroupKFold(n_splits=5).split(X, y, groups)):
        model = make_model()
        model.fit(X[tr], y[tr], sample_weight=weight[tr])
        oof[te] = model.predict_proba(X[te])[:, 1]
        log.info("fold %d AUC %.3f", fold, roc_auc_score(y[te], oof[te]))
    cv_auc = float(roc_auc_score(y, oof))
    log.info("spatial CV AUC %.3f", cv_auc)

    model = make_model()
    model.fit(X, y, sample_weight=weight)
    tmp_model = files.model.with_suffix(".tmp.json")
    model.save_model(tmp_model)
    tmp_model.replace(files.model)

    # The background is a uniform sample of land cells, so its predictions give
    # the distribution needed to turn probabilities into Norway-wide percentiles.
    reference = np.sort(model.predict_proba(X[y == 0])[:, 1])
    write_score(files.score, grid, species, model, reference)

    # With importance_type="gain" every value is a float.
    importance = cast(dict[str, float], model.get_booster().get_score(importance_type="gain"))
    total = sum(importance.values()) or 1.0
    names = model.get_booster().feature_names or [f"f{i}" for i in range(len(FEATURE_KEYS))]
    feature_importance = {
        FEATURE_KEYS[i]: round(importance.get(name, 0.0) / total, 4) for i, name in enumerate(names)
    }
    meta = {
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "resolution_m": grid.res,
        "species": species.key,
        "name": species.name,
        "latin": species.latin,
        "n_findings": findings.height,
        "n_findings_used": precise.height,
        "n_own_findings_used": n_own,
        "findings_max_uncertainty_m": config.FINDINGS_MAX_UNCERTAINTY_M,
        "n_presence_cells": n_pos,
        "n_background_cells": n_bg,
        "cv_auc": round(cv_auc, 4),
        "habitat": habitat_text(species),
        "feature_importance": feature_importance,
    }
    write_findings_geojson(files.findings, findings.filter(pl.col("source") == "gbif"))
    # Meta last: its training time tells the map that a new version is ready.
    tmp_meta = files.meta.with_suffix(".tmp.json")
    tmp_meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    tmp_meta.replace(files.meta)
    log.info("done: %s", json.dumps(feature_importance))


def write_score(
    path: Path,
    grid: Grid,
    species: config.Species,
    model: XGBClassifier,
    reference: np.ndarray,
) -> None:
    """Predict every habitat cell chunk by chunk and write percentile scores.

    Scores are stored as uint8 1..255 (0 = no data). Land outside the species'
    habitat gets 1, the lowest score. Written to a temporary file first, so the
    map keeps serving the previous version until the new one is complete.
    """
    meta = load_features_meta()
    tmp = path.with_suffix(".tmp.tif")
    with (
        rasterio.open(FEATURES_PATH) as fsrc,
        rasterio.open(LAND_PATH) as lsrc,
        rasterio.open(tmp, "w", **raster_profile(grid, 1, "uint8")) as dst,
    ):
        for i, (row0, col0, height, width) in enumerate(meta["land_chunks"]):
            window = Window(col0, row0, width, height)  # type: ignore[call-arg]
            land = lsrc.read(1, window=window).astype(bool)
            feats = fsrc.read(window=window)
            ok = land & habitat(species, feats)
            score = np.where(land, 1, 0).astype(np.uint8)
            if ok.any():
                proba = model.predict_proba(feats[:, ok].T)[:, 1]
                ranks = np.searchsorted(reference, proba) / len(reference)
                score[ok] = (1 + np.round(ranks * 254)).astype(np.uint8)
            dst.write(score, 1, window=window)
            if i % 100 == 0:
                log.info("scoring chunk %d/%d", i, len(meta["land_chunks"]))
    with rasterio.open(tmp, "r+") as dst:
        dst.build_overviews([2, 4, 8, 16, 32, 64], Resampling.nearest)
    tmp.replace(path)


def write_findings_geojson(path: Path, findings: pl.DataFrame) -> None:
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(r["lon"], 5), round(r["lat"], 5)]},
            "properties": {"year": r["year"]},
        }
        for r in findings.select("lon", "lat", "year").iter_rows(named=True)
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
