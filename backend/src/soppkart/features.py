"""Build per-cell feature rasters on the analysis grid.

Norway is processed in square chunks (about CHUNK_SIZE_M wide),
in parallel, so memory use doesn't grow with the resolution. Each chunk's
features are written straight into FEATURES_PATH, a multi-band GeoTIFF (one
band per feature, band description = feature key), and its land mask into
LAND_PATH. Chunks without land are skipped.
"""

import json
import logging
import math
import multiprocessing as mp
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pyogrio
import rasterio
import shapely
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.merge import merge
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds
from scipy import ndimage

from soppkart import config, sources
from soppkart.grid import Grid, block_reduce_mean

log = logging.getLogger(__name__)

# pyogrio tags geometry columns as geoarrow; polars can just keep the raw WKB.
os.environ.setdefault("POLARS_UNKNOWN_EXTENSION_TYPE_BEHAVIOR", "load_as_storage")

FEATURES_PATH = config.FEATURES_DIR / "features.tif"
LAND_PATH = config.FEATURES_DIR / "land.tif"
FEATURES_META_PATH = config.FEATURES_DIR / "meta.json"
SOIL_GPKG = config.FEATURES_DIR / "soil.gpkg"

# Chunks are ~33 km wide whatever the resolution, so memory per worker stays the same.
CHUNK_SIZE_M = 32_768
WORKERS = int(os.environ.get("SOPPKART_WORKERS", str(min(6, os.cpu_count() or 1))))

N50_GDB = "Basisdata_0000_Norge_25833_N50Kartdata_FGDB.gdb"
N50_AREA_LAYER = "N50_Arealdekke_omrade"
N50_LINE_LAYER = "N50_Arealdekke_senterlinje"

# Land cover classes used when rasterizing N50 (0 = no data, outside Norway).
LC_SEA, LC_FRESHWATER, LC_BUILT, LC_FARMLAND, LC_OPEN, LC_OTHER = 1, 2, 3, 4, 5, 6
LAND_COVER_CLASSES = {
    "Havflate": LC_SEA,
    "Innsjø": LC_FRESHWATER,
    "InnsjøRegulert": LC_FRESHWATER,
    "Elv": LC_FRESHWATER,
    # Towns, industry etc. are left out of the map: the map is about nature.
    "Tettbebyggelse": LC_BUILT,
    "BymessigBebyggelse": LC_BUILT,
    "Industriområde": LC_BUILT,
    "Lufthavn": LC_BUILT,
    "Steinbrudd": LC_BUILT,
    "Steintipp": LC_BUILT,
    "Alpinbakke": LC_BUILT,
    "Golfbane": LC_BUILT,
    "Gravplass": LC_BUILT,
    "Park": LC_BUILT,
    "Rullebane": LC_BUILT,
    "SportIdrettPlass": LC_BUILT,
    "DyrketMark": LC_FARMLAND,
    "ÅpentOmråde": LC_OPEN,
}
# Land cover is rasterized at this resolution, then aggregated to grid cells.
LAND_COVER_RES = 8
# Distance to water is computed with this much margin around each chunk, and
# capped at it: water further away than this is reported as this distance.
WATER_MAX_DIST_M = 2000
# Gaps in built-up areas narrower than about twice this (small parks, open
# lots between buildings) count as built-up too, so towns are masked whole.
BUILT_UP_CLOSING_M = 100
TERRAIN_HALO_M = 20


# (path, bounds) of each tile in a set of GeoTIFF tiles.
TileIndex = list[tuple[str, tuple[float, float, float, float]]]


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    unit: str
    categorical: bool = False


# Order matters: it is the band order in features.tif and the model's column order.
FEATURES = [
    Feature("pct_lauv", "Lauvskog", "%"),
    Feature("pct_furu", "Furuskog", "%"),
    Feature("pct_gran", "Granskog", "%"),
    Feature("pct_bland", "Blandingsskog", "%"),
    Feature("tree_age", "Skogens alder (snitt)", "år"),
    Feature("bonitet", "Bonitet (lav = næringsfattig, høy = næringsrik)", ""),
    Feature("pct_dyrket", "Dyrket mark", "%"),
    Feature("pct_apent", "Åpent område (lynghei, snaumark o.l.)", "%"),
    Feature("pct_beite", "Beite og kulturlandskap", "%"),
    Feature("pct_aker", "Åker (korn, poteter, grønnsaker)", "%"),
    Feature("pct_slatt", "Slått gress (traktor)", "%"),
    Feature("pct_uslatt", "Gress som ikke slås (beite o.l.)", "%"),
    Feature("elevation", "Gjennomsnittlig høyde", "moh"),
    Feature("slope", "Helning", "°"),
    Feature("aspect_east", "Skråning mot øst (−1 vest … 1 øst)", ""),
    Feature("aspect_north", "Skråning mot nord (−1 sør … 1 nord)", ""),
    Feature("soil", "Jordtype", "", categorical=True),
    Feature("dist_water", "Avstand til ferskvann", "m"),
    Feature("precip", "Nedbør juni–september", "mm"),
]
FEATURE_KEYS = [f.key for f in FEATURES]

# NGU løsmassetype codes grouped into broader soil classes. Index 0 = unknown.
SOIL_CLASSES: list[tuple[str, list[int]]] = [
    ("Ukjent", [0, 1]),
    ("Tykk morene", [10, 11, 13, 14, 15, 16, 17]),
    ("Tynn morene", [12]),
    ("Breelv-/sandavsetning", [20, 21, 22, 30, 31, 35, 36, 37, 60]),
    ("Hav- og fjordavsetning", [40, 41, 42, 43]),
    ("Elveavsetning", [50, 53, 54, 55, 56, 57]),
    ("Forvitringsmateriale", [70, 71, 72, 73]),
    ("Skredmateriale", [80, 81, 82, 88, *range(300, 322)]),
    ("Torv og myr", [90]),
    ("Tynt organisk dekke over berg", [100, 101, 102]),
    ("Bart fjell", [110, 130, 140]),
    ("Fyllmasse", [120, 121, 122]),
]
SOIL_NAMES = [name for name, _ in SOIL_CLASSES]


def read_layer(
    path: Path,
    layer: str,
    where: str | None = None,
    columns: list[str] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    skip_features: int = 0,
    max_features: int | None = None,
) -> pl.DataFrame:
    """Read a vector layer into polars with a 'geometry' column of shapely objects."""
    meta, table = pyogrio.read_arrow(
        path,
        layer=layer,
        where=where,
        columns=columns,
        bbox=bbox,
        skip_features=skip_features,
        max_features=max_features,
    )
    df = pl.from_arrow(table)
    assert isinstance(df, pl.DataFrame)
    geom_col = meta["geometry_name"] or "wkb_geometry"
    geoms = shapely.from_wkb(df[geom_col].to_numpy())
    return df.drop(geom_col).with_columns(pl.Series("geometry", geoms, dtype=pl.Object))


def geometries(df: pl.DataFrame) -> np.ndarray:
    """The geometry column as a writable numpy array (shapely needs to write to it)."""
    return np.array(df["geometry"].to_numpy(), dtype=object)


def clipped(df: pl.DataFrame, bounds: tuple[float, float, float, float]) -> np.ndarray:
    """Geometries clipped to bounds, so huge polygons (sea, big lakes) rasterize fast."""
    geoms = shapely.clip_by_rect(geometries(df), *bounds)
    return np.asarray(geoms[~shapely.is_empty(geoms)])


# --- One-time preparation ----------------------------------------------------


def prepare_soil() -> None:
    """Convert NGU løsmasser shapefiles to a GeoPackage with soil class and a spatial index.

    Shapefiles have no spatial index, so every chunk would otherwise scan all
    660k polygons.
    """
    if SOIL_GPKG.exists():
        return
    code_to_class = np.zeros(400, dtype=np.uint8)
    for class_idx, (_, codes) in enumerate(SOIL_CLASSES):
        code_to_class[codes] = class_idx
    SOIL_GPKG.parent.mkdir(parents=True, exist_ok=True)
    tmp = SOIL_GPKG.with_suffix(".tmp.gpkg")
    tmp.unlink(missing_ok=True)
    batch = 100_000
    for path in sorted(sources.LOSMASSER_DIR.rglob("LosmasseFlate*.shp")):
        n_features = pyogrio.read_info(path)["features"]
        for start in range(0, n_features, batch):
            log.info("converting soil polygons %d-%d of %d", start, start + batch, n_features)
            df = read_layer(
                path, path.stem, columns=["jordart"], skip_features=start, max_features=batch
            )
            classes = code_to_class[df["jordart"].fill_null(0).clip(0, 399).to_numpy()]
            pyogrio.raw.write(
                tmp,
                geometry=shapely.to_wkb(geometries(df)),
                field_data=[classes.astype(np.int32)],
                fields=["cls"],
                layer="soil",
                driver="GPKG",
                geometry_type="Polygon",
                crs=config.CRS,
                append=tmp.exists(),
            )
    tmp.rename(SOIL_GPKG)


def tile_index(directory: Path) -> TileIndex:
    """Paths and bounds of all GeoTIFF tiles in a directory tree."""
    index = []
    for path in sorted(directory.rglob("*.tif")):
        with rasterio.open(path) as src:
            index.append((str(path), tuple(src.bounds)))
    return index


def dtm_index() -> TileIndex:
    """Paths and bounds of all DTM 10 tiles."""
    return tile_index(sources.DTM10_DIR)


def overlapping(index: TileIndex, bounds: tuple[float, float, float, float]) -> list[str]:
    left, bottom, right, top = bounds
    return [p for p, (l, b, r, t) in index if l < right and r > left and b < top and t > bottom]


def load_precipitation() -> tuple[np.ndarray, Any]:
    """WorldClim precipitation summed over PREC_MONTHS, cropped around Norway."""
    total: np.ndarray | None = None
    transform = None
    for month in config.PREC_MONTHS:
        path = sources.PREC_DIR / f"wc2.1_30s_prec_{month:02d}.tif"
        with rasterio.open(path) as src:
            window = (
                from_bounds(2.0, 56.0, 34.0, 73.0, transform=src.transform)
                .round_offsets()
                .round_lengths()
            )
            arr = src.read(1, window=window).astype(np.float32)
            if src.nodata is not None:
                arr[arr == src.nodata] = np.nan
            transform = src.window_transform(window)
        total = arr if total is None else total + arr
    assert total is not None
    return total, transform


# --- Per-chunk feature computation -------------------------------------------

# Copernicus HRL values (see config.HRL_LAYERS).
GRAME_NOT_MOWED, GRAME_OUTSIDE = 0, 255  # 1-4 = times mowed, 253 = not grass
CTY_NOT_CROPLAND, CTY_OUTSIDE = 0, 65535  # anything else is a crop code

# Set in each worker process by init_worker.
_DTM_INDEX: TileIndex = []
_HRL_INDEX: dict[str, TileIndex] = {}
_PRECIP: tuple[np.ndarray, Any] | None = None
_TOWNS: shapely.STRtree | None = None


def init_worker(dtm: TileIndex, precip: tuple[np.ndarray, Any]) -> None:
    global _DTM_INDEX, _HRL_INDEX, _PRECIP, _TOWNS
    _DTM_INDEX = dtm
    _HRL_INDEX = {name: tile_index(sources.HRL_DIR / name) for name in config.HRL_LAYERS}
    _PRECIP = precip
    _TOWNS = shapely.STRtree(geometries(read_layer(sources.TETTSTED_PATH, "tettsteder")))


def land_cover_raster(fine: Grid) -> np.ndarray:
    """N50 land cover classes (LC_*) rasterized on a fine grid, rivers included."""
    gdb = sources.N50_DIR / N50_GDB
    bounds = fine.bounds
    raster = np.zeros(fine.shape, dtype=np.uint8)
    areas = read_layer(gdb, N50_AREA_LAYER, columns=["objtype"], bbox=bounds)
    if areas.height:
        classes = np.array(
            [LAND_COVER_CLASSES.get(t, LC_OTHER) for t in areas["objtype"]], dtype=np.uint8
        )
        geoms = shapely.clip_by_rect(geometries(areas), *bounds)
        keep = ~shapely.is_empty(geoms)
        # Draw plain land first so water and built-up areas on top of it win.
        order = np.argsort(classes != LC_OTHER, kind="stable")
        order = order[keep[order]]
        rasterize(
            zip(geoms[order], classes[order].tolist()),
            out=raster,
            transform=fine.transform,
        )
    # Land inside SSB towns and villages counts as built-up, parks included.
    assert _TOWNS is not None
    towns = shapely.clip_by_rect(
        _TOWNS.geometries.take(_TOWNS.query(shapely.box(*bounds))), *bounds
    )
    towns = towns[~shapely.is_empty(towns)]
    if len(towns):
        in_town = rasterize(
            ((g, 1) for g in towns),
            out_shape=fine.shape,
            transform=fine.transform,
            fill=0,
            dtype="uint8",
        ).astype(bool)
        raster[in_town & np.isin(raster, [LC_FARMLAND, LC_OPEN, LC_OTHER])] = LC_BUILT
    rivers = read_layer(gdb, N50_LINE_LAYER, where="objtype = 'ElvBekk'", bbox=bounds)
    if rivers.height:
        rasterize(
            ((g, LC_FRESHWATER) for g in clipped(rivers, bounds)),
            out=raster,
            transform=fine.transform,
            all_touched=True,
        )
    return raster


def close_built_up(lc: np.ndarray) -> None:
    """Mark land enclosed by built-up area and narrower than ~2x BUILT_UP_CLOSING_M as built-up.

    A morphological closing done with two distance transforms, which is much
    faster than binary_closing with a large structuring element.
    """
    built = lc == LC_BUILT
    if not built.any():
        return
    radius = BUILT_UP_CLOSING_M / LAND_COVER_RES
    dilated = ndimage.distance_transform_edt(~built) <= radius
    closed = ndimage.distance_transform_edt(dilated) > radius
    land = np.isin(lc, [LC_FARMLAND, LC_OPEN, LC_OTHER])
    lc[closed & land] = LC_BUILT


def share(blocks: np.ndarray, value: int | list[int]) -> np.ndarray:
    """Percent of sub-cells in each block with the given class(es)."""
    hit = np.isin(blocks, value)
    return hit.mean(axis=(1, 3), dtype=np.float32) * 100


def pasture(chunk: Grid) -> np.ndarray:
    """Percent pasture / farmed landscape per cell, from Corine Land Cover (25 ha minimum)."""
    fine = chunk.fine(chunk.res // LAND_COVER_RES)
    codes = ", ".join(f"'{c}'" for c in config.CLC_PASTURE_CODES)
    df = read_layer(
        sources.CLC_PATH, "clc", where=f"clc12_kode IN ({codes})", columns=[], bbox=fine.bounds
    )
    if not df.height:
        return np.zeros(chunk.shape, dtype=np.float32)
    raster = rasterize(
        ((g, 1) for g in clipped(df, fine.bounds)),
        out_shape=fine.shape,
        transform=fine.transform,
        fill=0,
        dtype="uint8",
    )
    factor = chunk.res // LAND_COVER_RES
    return share(raster.reshape(chunk.height, factor, chunk.width, factor), 1)


def hrl_raster(name: str, fine: Grid, outside: int, dtype: str) -> np.ndarray:
    """A Copernicus HRL layer (EPSG:3035, 10 m) resampled onto a fine grid.

    Pixels without data get the layer's `outside` value.
    """
    out = np.full(fine.shape, outside, dtype=dtype)
    bounds = transform_bounds(fine.crs, "EPSG:3035", *fine.bounds)
    files = overlapping(_HRL_INDEX[name], bounds)
    if not files:
        return out
    mosaic, transform = merge(files, bounds=bounds, nodata=outside)
    reproject(
        source=mosaic[0],
        destination=out,
        src_transform=transform,
        src_crs="EPSG:3035",
        src_nodata=outside,
        dst_transform=fine.transform,
        dst_crs=fine.crs,
        dst_nodata=outside,
        resampling=Resampling.nearest,
    )
    return out


def mowing_and_crops(chunk: Grid) -> dict[str, np.ndarray]:
    """Percent crop fields, mowed grass and unmowed grass per cell (Copernicus HRL).

    Mowed grass is hay/silage fields worked by tractors; grass that is never
    mowed is mostly pasture or natural grassland.
    """
    factor = chunk.res // LAND_COVER_RES
    fine = chunk.fine(factor)
    crops = hrl_raster("CTY", fine, CTY_OUTSIDE, "uint16")
    mowing = hrl_raster("GRAME", fine, GRAME_OUTSIDE, "uint8")
    is_crop = (crops != CTY_NOT_CROPLAND) & (crops != CTY_OUTSIDE)
    classes = np.zeros(fine.shape, dtype=np.uint8)
    classes[(mowing >= 1) & (mowing <= 4)] = 2
    classes[(mowing == GRAME_NOT_MOWED) & ~is_crop] = 3
    classes[is_crop] = 1
    blocks = classes.reshape(chunk.height, factor, chunk.width, factor)
    return {
        "pct_aker": share(blocks, 1),
        "pct_slatt": share(blocks, 2),
        "pct_uslatt": share(blocks, 3),
    }


def forest_composition(chunk: Grid) -> dict[str, np.ndarray]:
    """Forest per cell from SR16 (16 m): percent lauv/furu/gran/blandingsskog,
    plus mean tree age and bonitet of the forest pixels.

    SR16 only has a dominant species per 16 m pixel (1 gran, 2 furu, 3 lauv).
    A forest pixel counts as blandingsskog when its 3x3 neighbourhood (~48 m)
    contains more than one species; otherwise it counts towards its own species.
    """
    factor = chunk.res // 16
    with rasterio.open(sources.SR16_PATH) as src:
        col0 = round((chunk.x0 - src.transform.c) / 16)
        row0 = round((src.transform.f - chunk.y0) / 16)
        # One pixel of halo on each side for the 3x3 neighbourhood.
        window = Window(col0 - 1, row0 - 1, chunk.width * factor + 2, chunk.height * factor + 2)  # type: ignore[call-arg]
        species = src.read(1, window=window, boundless=True, fill_value=-9999)
    extra = {}
    # (key, raster, lowest valid value). Tree age band 1 is the estimate (bands
    # 2-3 are its uncertainty range). Bonitet 0 means unproductive forest, the
    # poorest kind, so it is a real value; age 0 is not.
    for key, path, lowest in (
        ("tree_age", sources.SR16_AGE_PATH, 1),
        ("bonitet", sources.SR16_BONITET_PATH, 0),
    ):
        with rasterio.open(path) as src:
            # Same pixel grid as the tree species raster.
            assert abs(src.transform.c - config.GRID_ORIGIN[0]) < 1e-6 and src.res == (16.0, 16.0)
            values = src.read(1, window=window, boundless=True, fill_value=-9999).astype(np.float32)
        # Only forest pixels count; the mean ignores the rest.
        values[(values < lowest) | (species <= 0)] = np.nan
        extra[key] = block_reduce_mean(values[1:-1, 1:-1], factor)
    has = [ndimage.maximum_filter(species == s, size=3) for s in (1, 2, 3)]
    n_species = has[0].astype(np.uint8) + has[1] + has[2]
    mixed = (species > 0) & (n_species >= 2)
    inner = (slice(1, -1), slice(1, -1))
    classes = {
        "pct_gran": (species == 1) & ~mixed,
        "pct_furu": (species == 2) & ~mixed,
        "pct_lauv": (species == 3) & ~mixed,
        "pct_bland": mixed,
    }
    shares = {
        key: pixels[inner]
        .reshape(chunk.height, factor, chunk.width, factor)
        .mean(axis=(1, 3), dtype=np.float32)
        * 100
        for key, pixels in classes.items()
    }
    return shares | extra


def terrain(chunk: Grid) -> dict[str, np.ndarray]:
    """Mean elevation, slope and aspect components from Kartverket DTM 10.

    Slope and aspect are computed at 10 m and then averaged, so they describe
    the terrain inside each cell. Aspect is stored as east/north components of
    the downslope direction, set to zero on flat ground (< 2°), so the cell mean
    also reflects how consistently the cell faces one way.
    """
    left, bottom, right, top = chunk.bounds
    bounds = (
        left - TERRAIN_HALO_M,
        bottom - TERRAIN_HALO_M,
        right + TERRAIN_HALO_M,
        top + TERRAIN_HALO_M,
    )
    files = overlapping(_DTM_INDEX, bounds)
    keys = ("elevation", "slope", "aspect_east", "aspect_north")
    out = {k: np.full(chunk.shape, np.nan, dtype=np.float32) for k in keys}
    if not files:
        return out
    mosaic, transform = merge(files, bounds=bounds, res=10, nodata=np.nan, dtype="float32")
    z = mosaic[0]
    z[z < -1000] = np.nan
    dz_drow, dz_dcol = np.gradient(z, 10.0)
    dz_east = dz_dcol
    dz_north = -dz_drow
    slope = np.degrees(np.arctan(np.hypot(dz_east, dz_north)))
    aspect = np.arctan2(-dz_east, -dz_north)
    flat = slope < 2.0
    values = {
        "elevation": z,
        "slope": slope,
        "aspect_east": np.where(flat, 0.0, np.sin(aspect)),
        "aspect_north": np.where(flat, 0.0, np.cos(aspect)),
    }
    for key, arr in values.items():
        reproject(
            source=arr.astype(np.float32),
            destination=out[key],
            src_transform=transform,
            src_crs=chunk.crs,
            src_nodata=np.nan,
            dst_transform=chunk.transform,
            dst_crs=chunk.crs,
            dst_nodata=np.nan,
            resampling=Resampling.average,
        )
    return out


def soil_type(chunk: Grid) -> np.ndarray:
    """Dominant grouped NGU soil class per cell (index into SOIL_NAMES)."""
    factor = chunk.fine_factor(16)
    fine = chunk.fine(factor)
    raster = np.zeros(fine.shape, dtype=np.uint8)
    df = read_layer(SOIL_GPKG, "soil", bbox=fine.bounds)
    if df.height:
        geoms = shapely.clip_by_rect(geometries(df), *fine.bounds)
        keep = ~shapely.is_empty(geoms)
        rasterize(
            zip(geoms[keep], df["cls"].to_numpy()[keep].tolist()),
            out=raster,
            transform=fine.transform,
        )
    # Majority vote over the factor x factor sub-cells, ignoring "unknown".
    blocks = raster.reshape(chunk.height, factor, chunk.width, factor)
    counts = np.stack(
        [(blocks == c).sum(axis=(1, 3), dtype=np.uint16) for c in range(1, len(SOIL_CLASSES))]
    )
    dominant = counts.argmax(axis=0) + 1
    dominant[counts.max(axis=0) == 0] = 0
    return dominant.astype(np.float32)


def precipitation(chunk: Grid) -> np.ndarray:
    """Normal precipitation (mm) summed over PREC_MONTHS, from WorldClim 2.1."""
    assert _PRECIP is not None
    total, transform = _PRECIP
    out = np.full(chunk.shape, np.nan, dtype=np.float32)
    reproject(
        source=total,
        destination=out,
        src_transform=transform,
        src_crs="EPSG:4326",
        src_nodata=np.nan,
        dst_transform=chunk.transform,
        dst_crs=chunk.crs,
        dst_nodata=np.nan,
        resampling=Resampling.bilinear,
    )
    return out


def process_chunk(chunk: Grid) -> tuple[Grid, np.ndarray | None, np.ndarray | None]:
    """All features for one chunk: (chunk, features [band, row, col], land mask).

    Returns None arrays for chunks without land.
    """
    halo = math.ceil(WATER_MAX_DIST_M / chunk.res)
    wide = chunk.expand(halo)
    factor = chunk.res // LAND_COVER_RES
    lc_wide = land_cover_raster(wide.fine(factor))
    close_built_up(lc_wide)
    inner = slice(halo * factor, -halo * factor)
    lc = lc_wide[inner, inner]
    blocks = lc.reshape(chunk.height, factor, chunk.width, factor)
    land = share(blocks, [LC_FARMLAND, LC_OPEN, LC_OTHER]) > 50
    if not land.any():
        return chunk, None, None

    bands: dict[str, np.ndarray] = {}
    bands.update(forest_composition(chunk))
    bands["pct_dyrket"] = share(blocks, LC_FARMLAND)
    bands["pct_apent"] = share(blocks, LC_OPEN)
    bands["pct_beite"] = pasture(chunk)
    bands.update(mowing_and_crops(chunk))
    bands.update(terrain(chunk))
    bands["soil"] = soil_type(chunk)

    water = lc_wide == LC_FRESHWATER
    if water.any():
        dist = ndimage.distance_transform_edt(~water).astype(np.float32) * LAND_COVER_RES
        dist = np.minimum(dist, WATER_MAX_DIST_M)
    else:
        dist = np.full(lc_wide.shape, WATER_MAX_DIST_M, dtype=np.float32)
    bands["dist_water"] = block_reduce_mean(dist[inner, inner], factor)
    bands["precip"] = precipitation(chunk)

    stack = np.stack([bands[key] for key in FEATURE_KEYS]).astype(np.float32)
    stack[:, ~land] = np.nan
    return chunk, stack, land


# --- Orchestration -----------------------------------------------------------


def chunk_cells(grid: Grid) -> int:
    return max(1, CHUNK_SIZE_M // grid.res)


def chunks(grid: Grid) -> list[Grid]:
    size = chunk_cells(grid)
    return [
        grid.subgrid(row, col, min(size, grid.height - row), min(size, grid.width - col))
        for row in range(0, grid.height, size)
        for col in range(0, grid.width, size)
    ]


def raster_profile(grid: Grid, count: int, dtype: str) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": count,
        "dtype": dtype,
        "crs": grid.crs,
        "transform": grid.transform,
        "compress": "deflate",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "BIGTIFF": "YES",
        "nodata": math.nan if dtype.startswith("float") else 0,
    }
    if dtype.startswith("float"):
        profile["predictor"] = 3
    return profile


def build_features(grid: Grid) -> None:
    """Compute all features chunk by chunk and write features.tif and land.tif."""
    log.info("grid: %dx%d cells at %d m", grid.width, grid.height, grid.res)
    if grid.res % LAND_COVER_RES or grid.res < 16:
        raise ValueError("resolution must be a multiple of 16")
    prepare_soil()
    dtm = dtm_index()
    precip = load_precipitation()
    todo = chunks(grid)
    log.info("%d chunks of %d cells, %d workers", len(todo), chunk_cells(grid), WORKERS)

    FEATURES_PATH.parent.mkdir(parents=True, exist_ok=True)
    land_chunks = []
    n_land = 0
    with (
        rasterio.open(FEATURES_PATH, "w", **raster_profile(grid, len(FEATURES), "float32")) as fdst,
        rasterio.open(LAND_PATH, "w", **raster_profile(grid, 1, "uint8")) as ldst,
        mp.get_context("fork").Pool(
            WORKERS, initializer=init_worker, initargs=(dtm, precip)
        ) as pool,
    ):
        for i, key in enumerate(FEATURE_KEYS, start=1):
            fdst.set_band_description(i, key)
        for done, (chunk, stack, land) in enumerate(
            pool.imap_unordered(process_chunk, todo), start=1
        ):
            if stack is not None and land is not None:
                window = grid.window_of(chunk)
                fdst.write(stack, window=window)
                ldst.write(land.astype(np.uint8), 1, window=window)
                land_chunks.append(grid.chunk_offsets(chunk))
                n_land += int(land.sum())
            if done % 50 == 0 or done == len(todo):
                log.info("chunks: %d/%d done, %d with land", done, len(todo), len(land_chunks))

    meta = {
        "resolution_m": grid.res,
        "n_land_cells": n_land,
        "chunk_cells": chunk_cells(grid),
        "land_chunks": sorted(land_chunks),
    }
    FEATURES_META_PATH.write_text(json.dumps(meta))
    log.info("wrote %s (%d land cells)", FEATURES_PATH, n_land)


def load_features_meta() -> dict[str, Any]:
    return dict(json.loads(FEATURES_META_PATH.read_text()))
