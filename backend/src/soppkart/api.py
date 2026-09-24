"""FastAPI app serving score tiles, point lookups and findings per species."""

import json
import math
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from pyproj import Transformer
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader

from soppkart import config, features, userfindings
from soppkart.colormap import DEFAULT_THRESHOLD, colormap
from soppkart.train import ModelFiles, model_files

app = FastAPI(title="Soppkart")

TO_GRID = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
CACHE_HEADERS = {"Cache-Control": "public, max-age=3600"}

# Retraining runs as a separate process per species (see /retrain).
TRAINING: dict[str, subprocess.Popen[bytes]] = {}


def is_training(species: str) -> bool:
    proc = TRAINING.get(species)
    return proc is not None and proc.poll() is None


def get_files(species: str) -> ModelFiles:
    if species not in config.SPECIES:
        raise HTTPException(404, f"Ukjent art: {species}")
    return model_files(species)


def is_ready(files: ModelFiles) -> bool:
    return files.score.exists() and files.meta.exists() and features.FEATURES_PATH.exists()


@lru_cache(maxsize=16)
def load_meta(path: Path, mtime: float) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def meta(files: ModelFiles) -> dict[str, Any]:
    return load_meta(files.meta, files.meta.stat().st_mtime)


@app.get("/api/species")
def species_list() -> list[dict[str, Any]]:
    return [
        {"key": s.key, "name": s.name, "latin": s.latin, "ready": is_ready(model_files(s.key))}
        for s in config.SPECIES.values()
    ]


@app.get("/api/{species}/status")
def status(species: str) -> dict[str, Any]:
    files = get_files(species)
    extra = {"training": is_training(species), "my_findings": len(userfindings.list_for(species))}
    if not is_ready(files):
        return {
            "ready": False,
            "message": "Ingen modell ennå. Kjør: docker compose run --rm backend soppkart all",
            "model": None,
            **extra,
        }
    return {"ready": True, "message": "ok", "model": meta(files), **extra}


@lru_cache(maxsize=4096)
def render_tile(path: Path, z: int, x: int, y: int, threshold: float, mtime: float) -> bytes | None:
    with Reader(str(path)) as src:
        try:
            img = src.tile(x, y, z, tilesize=256)
        except TileOutsideBounds:
            return None
    if not img.mask.any():
        return None
    return bytes(img.render(img_format="PNG", colormap=colormap(threshold)))


@app.get("/api/{species}/tiles/{z}/{x}/{y}.png")
def tile(
    species: str,
    z: int,
    x: int,
    y: int,
    threshold: float = Query(DEFAULT_THRESHOLD, ge=0, le=0.99),
) -> Response:
    """Score tile. Scores below threshold (0..1) are transparent."""
    files = get_files(species)
    if not is_ready(files):
        return Response(status_code=204)
    threshold = round(threshold, 2)
    png = render_tile(files.score, z, x, y, threshold, files.score.stat().st_mtime)
    if png is None:
        return Response(status_code=204, headers=CACHE_HEADERS)
    return Response(png, media_type="image/png", headers=CACHE_HEADERS)


def read_pixel(path: Path, x: float, y: float) -> np.ndarray | None:
    with rasterio.open(path) as src:
        row, col = src.index(x, y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return None
        return src.read(window=((row, row + 1), (col, col + 1)))[:, 0, 0]


def display_value(key: str, value: float) -> float | str | None:
    if math.isnan(value):
        return None
    if key == "soil":
        return features.SOIL_NAMES[int(value)]
    return round(float(value), 2)


@app.get("/api/{species}/point")
def point(
    species: str,
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
) -> dict[str, Any]:
    files = get_files(species)
    if not is_ready(files):
        raise HTTPException(503, "Modellen er ikke trent ennå")
    x, y = TO_GRID.transform(lon, lat)
    values = read_pixel(features.FEATURES_PATH, x, y)
    score_px = read_pixel(files.score, x, y)
    if values is None or score_px is None:
        return {"lat": lat, "lon": lon, "inside": False, "score": None, "features": []}
    score_value = int(score_px[0])
    return {
        "lat": lat,
        "lon": lon,
        "inside": True,
        "score": None if score_value == 0 else round((score_value - 1) / 254, 3),
        "features": [
            {
                "key": f.key,
                "label": f.label,
                "unit": f.unit,
                "value": display_value(f.key, float(v)),
            }
            for f, v in zip(features.FEATURES, values, strict=True)
        ],
    }


@app.get("/api/{species}/findings.geojson")
def findings(species: str) -> FileResponse:
    files = get_files(species)
    if not files.findings.exists():
        raise HTTPException(404, "Ingen funn lastet ned ennå")
    return FileResponse(files.findings, media_type="application/geo+json")


class NewFinding(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # GPS accuracy in metres; None when the spot was picked on the map.
    accuracy_m: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)


def finding_feature(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [f["lon"], f["lat"]]},
        "properties": {k: f[k] for k in ("id", "accuracy_m", "found_at", "note")},
    }


@app.get("/api/{species}/my-findings")
def my_findings(species: str) -> dict[str, Any]:
    get_files(species)
    return {
        "type": "FeatureCollection",
        "features": [finding_feature(f) for f in userfindings.list_for(species)],
    }


@app.post("/api/{species}/my-findings")
def add_my_finding(species: str, finding: NewFinding) -> dict[str, Any]:
    get_files(species)
    # A spot picked on the map is where you say it is.
    accuracy = finding.accuracy_m if finding.accuracy_m is not None else 5.0
    added = userfindings.add(species, finding.lat, finding.lon, accuracy, finding.note)
    return finding_feature(added)


@app.delete("/api/my-findings/{finding_id}")
def delete_my_finding(finding_id: int) -> dict[str, bool]:
    if not userfindings.delete(finding_id):
        raise HTTPException(404, "Fant ikke funnet")
    return {"deleted": True}


@app.post("/api/{species}/retrain")
def retrain(species: str) -> dict[str, bool]:
    """Retrain the species' model in the background, with your own findings."""
    get_files(species)
    if is_training(species):
        raise HTTPException(409, "Modellen trenes allerede")
    log_path = config.DATA_DIR / "user" / f"train_{species}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log_file:
        TRAINING[species] = subprocess.Popen(
            ["soppkart", "train", "--species", species],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    return {"started": True}
