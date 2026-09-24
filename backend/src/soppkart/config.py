"""Paths, grid settings and data source URLs."""

import os
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("SOPPKART_DATA_DIR", "data")).resolve()
RAW_DIR = DATA_DIR / "raw"
FEATURES_DIR = DATA_DIR / "features"
MODEL_DIR = DATA_DIR / "model"

CRS = "EPSG:25833"

# ArcGIS Location Platform API key for the aerial photo background (Esri World
# Imagery). Optional: without it the map only offers Kartverket's topo map. The
# backend fetches the tiles itself, so the key never reaches the browser.
ESRI_API_KEY = os.environ.get("ESRI_API_KEY") or None
# Sent as Referer to Esri, for keys restricted to a site (e.g. https://soppkart.nielth.com).
ESRI_REFERER = os.environ.get("ESRI_REFERER") or None
# Redis for caching aerial photo tiles (optional; without it every tile goes to Esri).
REDIS_URL = os.environ.get("REDIS_URL") or None
# Used when Esri doesn't say how long a tile may be cached (it normally says 24 h).
IMAGERY_CACHE_DEFAULT_S = 86_400

ESRI_IMAGERY_URL = (
    "https://ibasemaps-api.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
)

# Grid cell size in metres. Must be a multiple of 16 so SR16 (16 m) pixels
# aggregate exactly into grid cells.
RESOLUTION = int(os.environ.get("SOPPKART_RESOLUTION", "32"))

# Grid extent in EPSG:25833: the SR16 extent, which covers mainland Norway.
# The grid origin is the SR16 origin so 16 m pixels line up with grid cells.
GRID_ORIGIN = (-99_513.01566065871, 8_000_000.0)
GRID_SIZE_M = (90_016 * 16, 100_299 * 16)


@dataclass(frozen=True)
class Species:
    key: str
    name: str
    latin: str
    # GBIF taxon keys whose occurrences count as findings of this species.
    taxon_keys: list[int]
    # Habitat rules: cells outside these forest shares (lauv + furu + gran +
    # blandingsskog, in %) are not habitat. They get the lowest score and are
    # left out of training, so the model learns what matters within the habitat.
    min_forest_pct: float | None = None
    max_forest_pct: float | None = None
    # Max share (%) of crop fields + mowed grass (Copernicus HRL): land worked by tractors.
    max_cultivated_pct: float | None = None


SPECIES = {
    s.key: s
    for s in [
        # Includes blek kantarell (C. pallens), which older records often lump in.
        # A forest mushroom: 90% of findings are in cells with 75%+ forest.
        Species(
            "kantarell",
            "Kantarell",
            "Cantharellus cibarius",
            [5249504, 5249496],
            min_forest_pct=25,
        ),
        # Grows in grazed grassland: not in forest, and not on fields worked by
        # tractors (crops, grass mowed for hay/silage).
        Species(
            "spiss_fleinsopp",
            "Spiss fleinsopp",
            "Psilocybe semilanceata",
            [5242507],
            max_forest_pct=50,
            max_cultivated_pct=50,
        ),
    ]
}

GBIF_MAX_UNCERTAINTY_M = 1000
# Findings you register yourself count this many times as much in training
# as a GBIF finding: you know they are real and precisely placed.
OWN_FINDING_WEIGHT = float(os.environ.get("SOPPKART_OWN_FINDING_WEIGHT", "3"))
# Only findings located at least this precisely are used for training, so a
# finding lands in the right grid cell. Findings without a stated precision are
# left out too.
FINDINGS_MAX_UNCERTAINTY_M = int(
    os.environ.get("SOPPKART_FINDINGS_MAX_UNCERTAINTY_M", str(max(50, RESOLUTION * 3 // 2)))
)

SR16_ZIP_URL = "https://kart8.nibio.no/uttak_Download/sr16raster/0000_25833_sr16_RASTER.zip"
SR16_MEMBER = "SRRTRESLAG.tif"
# Tree age (years) and bonitet (site index: how productive/nutrient-rich the forest is).
SR16_AGE_MEMBER = "SRRTREALDER.tif"
SR16_BONITET_MEMBER = "SRRBONITET.tif"

N50_URL = (
    "https://nedlasting.geonorge.no/geonorge/Basisdata/N50Kartdata/FGDB/"
    "Basisdata_0000_Norge_25833_N50Kartdata_FGDB.zip"
)

# Kartverket DTM 10 (10 m terrain model), delivered as 50 x 50 km tiles.
GEONORGE_API = "https://nedlasting.geonorge.no"
DTM10_UUID = "dddbb667-1303-4ac5-8640-7ec04c0e3918"
DTM10_URL = (
    "https://nedlasting.geonorge.no/geonorge/Basisdata/DTM10UTM33/TIFF/"
    "Basisdata_{code}_Celle_25833_DTM10UTM33_TIFF.zip"
)

# SSB tettsteder: outlines of towns and villages (200+ residents).
SSB_TETTSTED_URL = "https://kart.ssb.no/api/ogc/v1/tettsted_ogc_api/collections/tettsted_2026/items"

# Corine Land Cover 2012 for Norway (NIBIO, NLOD). Pasture is 231, but in
# Norway most grazed land is mapped as 243 (agriculture with significant
# natural vegetation), so both count as pasture / farmed landscape.
CLC_WFS_URL = "https://wfs.nibio.no/cgi-bin/clc2012"
CLC_PASTURE_CODES = ["231", "243"]

# Copernicus high resolution layers (10 m, yearly) from the Copernicus Data Space
# Ecosystem. Needs free S3 keys in CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY.
# Grassland Mowing Events tells mowed fields (tractors) from pasture, and Crop
# Types marks fields with grain, potatoes, vegetables etc.
CDSE_S3_ENDPOINT = "https://eodata.dataspace.copernicus.eu"
HRL_YEAR = 2024
HRL_LAYERS = {
    "GRAME": "grassland_mowing_events/clms_vlcc_grassland-mowing-events_europe_10m_yearly_v1",
    "CTY": "crop_types/clms_vlcc_crop-types_europe_10m_yearly_v1",
}

NGU_API = "https://nedlasting.ngu.no"
NGU_LOSMASSER_UUID = "3de4ddf6-d6b8-4398-8222-f5c47791a757"

WORLDCLIM_PREC_URL = "https://geodata.ucdavis.edu/climate/worldclim/2_1/base/wc2.1_30s_prec.zip"
# June-September, the main growth and fruiting season.
PREC_MONTHS = [6, 7, 8, 9]
