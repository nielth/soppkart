"""Fetch raw source datasets into RAW_DIR."""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import polars as pl
import pyogrio

from soppkart import config
from soppkart.download import (
    download_and_unzip,
    extract_remote_zip_member,
    order_geonorge_download,
)

log = logging.getLogger(__name__)

SR16_PATH = config.RAW_DIR / "sr16_treslag.tif"
SR16_AGE_PATH = config.RAW_DIR / "sr16_trealder.tif"
SR16_BONITET_PATH = config.RAW_DIR / "sr16_bonitet.tif"
N50_DIR = config.RAW_DIR / "n50"
DTM10_DIR = config.RAW_DIR / "dtm10"
TETTSTED_PATH = config.RAW_DIR / "tettsteder.geojson"
CLC_PATH = config.RAW_DIR / "clc2012.gpkg"
HRL_DIR = config.RAW_DIR / "hrl"
LOSMASSER_DIR = config.RAW_DIR / "losmasser"
PREC_DIR = config.RAW_DIR / "worldclim"
# Findings are training data, not raw downloads: kept outside RAW_DIR, which
# can be a separate volume that only matters when rebuilding features.
FINDINGS_DIR = config.DATA_DIR / "findings"


def fetch_sr16() -> Path:
    """Tree species, age and bonitet (SR16, 16 m) for all of Norway, from the national zip."""
    extract_remote_zip_member(config.SR16_ZIP_URL, config.SR16_AGE_MEMBER, SR16_AGE_PATH)
    extract_remote_zip_member(config.SR16_ZIP_URL, config.SR16_BONITET_MEMBER, SR16_BONITET_PATH)
    return extract_remote_zip_member(config.SR16_ZIP_URL, config.SR16_MEMBER, SR16_PATH)


def fetch_n50() -> Path:
    """Kartverket N50 (1:50 000): land cover, lakes, rivers and built-up areas."""
    return download_and_unzip(config.N50_URL, config.RAW_DIR / "n50.zip", N50_DIR)


def fetch_dtm10() -> Path:
    """Kartverket DTM 10: 10 m terrain model for all of Norway (~10 GB)."""
    res = httpx.get(f"{config.GEONORGE_API}/api/codelists/area/{config.DTM10_UUID}", timeout=60)
    res.raise_for_status()
    codes = [area["code"] for area in res.json()]
    log.info("fetching %d DTM 10 tiles", len(codes))

    def fetch_tile(code: str) -> None:
        out_dir = DTM10_DIR / code
        download_and_unzip(config.DTM10_URL.format(code=code), DTM10_DIR / f"{code}.zip", out_dir)

    DTM10_DIR.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch_tile, codes))
    return DTM10_DIR


def fetch_tettsteder() -> Path:
    """SSB tettsteder (towns and villages) as GeoJSON in EPSG:25833."""
    if TETTSTED_PATH.exists():
        log.info("cached: %s", TETTSTED_PATH.name)
        return TETTSTED_PATH
    features: list[dict[str, object]] = []
    url: str | None = config.SSB_TETTSTED_URL
    params: dict[str, str] | None = {
        "limit": "500",
        "crs": "http://www.opengis.net/def/crs/EPSG/0/25833",
    }
    with httpx.Client(timeout=120, headers={"Accept": "application/geo+json"}) as client:
        while url:
            res = client.get(url, params=params)
            res.raise_for_status()
            page = res.json()
            features.extend(page["features"])
            url = next((link["href"] for link in page["links"] if link["rel"] == "next"), None)
            params = None  # the next link already carries the query
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::25833"}},
        "features": features,
    }
    TETTSTED_PATH.write_text(json.dumps(collection))
    log.info("saved %d tettsteder", len(features))
    return TETTSTED_PATH


def fetch_clc() -> Path:
    """Corine Land Cover 2012 polygons for Norway, paged from NIBIO's WFS into a GeoPackage."""
    if CLC_PATH.exists():
        log.info("cached: %s", CLC_PATH.name)
        return CLC_PATH
    tmp = CLC_PATH.with_suffix(".tmp.gpkg")
    tmp.unlink(missing_ok=True)
    page_path = config.RAW_DIR / "clc_page.gml"
    page_size = 5000
    start = 0
    with httpx.Client(timeout=300) as client:
        while True:
            res = client.get(
                config.CLC_WFS_URL,
                params={
                    "SERVICE": "WFS",
                    "VERSION": "2.0.0",
                    "REQUEST": "GetFeature",
                    "TYPENAMES": "ms:CLC2012",
                    "SRSNAME": "EPSG:25833",
                    "COUNT": str(page_size),
                    "STARTINDEX": str(start),
                },
            )
            res.raise_for_status()
            page_path.write_bytes(res.content)
            meta, table = pyogrio.read_arrow(page_path, columns=["clc12_kode"])
            if table.num_rows == 0:
                break
            pyogrio.write_arrow(
                table,
                tmp,
                layer="clc",
                driver="GPKG",
                geometry_name=meta["geometry_name"] or "geometry",
                geometry_type=meta["geometry_type"],
                crs=config.CRS,
                append=tmp.exists(),
            )
            log.info("CLC: %d features", start + table.num_rows)
            start += page_size
    page_path.unlink(missing_ok=True)
    tmp.rename(CLC_PATH)
    return CLC_PATH


def hrl_tiles_for_norway() -> set[str]:
    """Names (E##N##) of the 100 km EPSG:3035 tiles overlapping the Norway grid."""
    from rasterio.warp import transform_bounds

    from soppkart.grid import Grid

    left, bottom, right, top = transform_bounds(config.CRS, "EPSG:3035", *Grid.norway().bounds)
    return {
        f"E{e:02d}N{n:02d}"
        for e in range(int(left // 100_000), int(right // 100_000) + 1)
        for n in range(int(bottom // 100_000), int(top // 100_000) + 1)
    }


def fetch_hrl() -> Path:
    """Copernicus Grassland Mowing Events and Crop Types tiles over Norway (HRL_YEAR)."""
    import boto3

    key_id = os.environ.get("CDSE_S3_ACCESS_KEY")
    secret = os.environ.get("CDSE_S3_SECRET_KEY")
    if not key_id or not secret:
        raise RuntimeError(
            "Set CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY (free Copernicus Data Space keys) in .env"
        )
    s3 = boto3.client(
        "s3",
        endpoint_url=config.CDSE_S3_ENDPOINT,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="default",
    )
    wanted = hrl_tiles_for_norway()
    for name, path in config.HRL_LAYERS.items():
        out_dir = HRL_DIR / name
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"CLMS/landcover_landuse/{path}/{config.HRL_YEAR}/01/01/"
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket="eodata", Prefix=prefix, Delimiter="/"
        )
        stems = [
            p["Prefix"].rstrip("/").split("/")[-1]
            for page in pages
            for p in page.get("CommonPrefixes", [])
        ]
        todo = [stem for stem in stems if stem.split("_")[5] in wanted]
        log.info("HRL %s: %d tiles over Norway", name, len(todo))
        for stem in todo:
            dest = out_dir / f"{stem.split('_')[5]}.tif"
            if not dest.exists():
                tmp = dest.with_suffix(".part")
                s3.download_file("eodata", f"{prefix}{stem}/{stem}.tif", str(tmp))
                tmp.rename(dest)
    return HRL_DIR


def fetch_losmasser() -> Path:
    """NGU løsmasser (superficial deposits) for all of Norway.

    Shapefile rather than FileGDB: the geodatabase contains curved polygons
    that shapely can't read, while the shapefile export has them linearized.
    """
    if (LOSMASSER_DIR / ".extracted").exists():
        return LOSMASSER_DIR
    url = order_geonorge_download(
        config.NGU_API,
        config.NGU_LOSMASSER_UUID,
        {"code": "0000", "type": "landsdekkende", "name": "Landsdekkende"},
        "25833",
        "SHAPE",
    )
    return download_and_unzip(url, config.RAW_DIR / "losmasser.zip", LOSMASSER_DIR)


def fetch_precipitation() -> Path:
    """WorldClim 2.1 monthly precipitation normals (30 arc-seconds) for the summer months."""
    for month in config.PREC_MONTHS:
        member = f"wc2.1_30s_prec_{month:02d}.tif"
        extract_remote_zip_member(config.WORLDCLIM_PREC_URL, member, PREC_DIR / member)
    return PREC_DIR


def findings_path(species: config.Species) -> Path:
    return FINDINGS_DIR / f"{species.key}.parquet"


def fetch_findings(species: config.Species) -> Path:
    """Occurrences of a species in Norway from GBIF (includes Artsdatabanken data)."""
    path = findings_path(species)
    if path.exists():
        log.info("cached: %s", path.name)
        return path
    rows: list[dict[str, object]] = []
    limit = 300
    with httpx.Client(timeout=60) as client:
        for taxon_key in species.taxon_keys:
            offset = 0
            while True:
                res = client.get(
                    "https://api.gbif.org/v1/occurrence/search",
                    params={
                        "taxonKey": taxon_key,
                        "country": "NO",
                        "hasCoordinate": "true",
                        "hasGeospatialIssue": "false",
                        "occurrenceStatus": "PRESENT",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                res.raise_for_status()
                page = res.json()
                for r in page["results"]:
                    rows.append(
                        {
                            "gbif_id": r["key"],
                            "taxon_key": taxon_key,
                            "lat": r["decimalLatitude"],
                            "lon": r["decimalLongitude"],
                            "year": r.get("year"),
                            "uncertainty_m": r.get("coordinateUncertaintyInMeters"),
                            "basis": r.get("basisOfRecord"),
                        }
                    )
                log.info("GBIF taxon %s: %d/%d", taxon_key, len(rows), page["count"])
                if page["endOfRecords"]:
                    break
                offset += limit

    df = pl.DataFrame(rows, infer_schema_length=None).filter(
        pl.col("uncertainty_m").is_null()
        | (pl.col("uncertainty_m") <= config.GBIF_MAX_UNCERTAINTY_M)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)
    log.info("saved %d %s findings", df.height, species.key)
    return path


def fetch_all() -> None:
    for species in config.SPECIES.values():
        fetch_findings(species)
    fetch_n50()
    fetch_tettsteder()
    fetch_clc()
    fetch_hrl()
    fetch_dtm10()
    fetch_sr16()
    fetch_losmasser()
    fetch_precipitation()
