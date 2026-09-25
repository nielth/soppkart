"""FastAPI app serving score tiles, point lookups and findings per species."""

import base64
import json
import math
import re
import subprocess
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, cast
from xml.etree import ElementTree

import httpx
import numpy as np
import rasterio
import redis.asyncio as redis
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from pyproj import Transformer
from rasterio.windows import Window
from rio_tiler.errors import TileOutsideBounds
from rio_tiler.io import Reader
from rio_tiler.utils import render

from soppkart import auth, config, features, stravaheat, userfindings
from soppkart.auth import User
from soppkart.colormap import DEFAULT_THRESHOLD, colormap
from soppkart.train import CONTRIB_NODATA, CONTRIB_SCALE, ModelFiles, model_files

app = FastAPI(title="Soppkart")

TO_GRID = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
CACHE_HEADERS = {"Cache-Control": "public, max-age=3600"}

# Retraining runs as a separate process per species (see /retrain).
TRAINING: dict[str, subprocess.Popen[bytes]] = {}


def is_training(species: str) -> bool:
    proc = TRAINING.get(species)
    return proc is not None and proc.poll() is None


def current_user(request: Request) -> User | None:
    """The logged-in user, or None for visitors."""
    return auth.user_for_token(request.cookies.get(auth.COOKIE_NAME))


def require_user(user: Annotated[User | None, Depends(current_user)]) -> User:
    if user is None:
        raise HTTPException(401, "Du må logge inn")
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Bare for admin")
    return user


# Endpoint parameters: the visitor (None if not logged in), a logged-in user, an admin.
MaybeUser = Annotated[User | None, Depends(current_user)]
LoggedIn = Annotated[User, Depends(require_user)]
Admin = Annotated[User, Depends(require_admin)]


def require_permission(user: User | None, permission: str) -> None:
    if user is None:
        raise HTTPException(401, "Du må logge inn")
    if not user.can(permission):
        raise HTTPException(403, "Du har ikke tilgang til dette")


def get_files(species: str, user: User | None) -> ModelFiles:
    if species not in config.SPECIES:
        raise HTTPException(404, f"Ukjent art: {species}")
    if config.SPECIES[species].restricted:
        require_permission(user, species)
    return model_files(species)


def cache_headers(species: str) -> dict[str, str]:
    """Restricted species' tiles may only be cached by the user's own browser."""
    if config.SPECIES[species].restricted:
        return {"Cache-Control": "private, max-age=3600"}
    return CACHE_HEADERS


def is_ready(files: ModelFiles) -> bool:
    return (
        files.contrib.exists()
        and files.reference.exists()
        and files.meta.exists()
        and features.FEATURES_PATH.exists()
    )


Weights = tuple[float, ...]
N_GROUPS = len(features.FEATURE_GROUPS)


def parse_weights(w: str | None) -> Weights:
    """Weights per feature group from "1,0.5,..." (FEATURE_GROUPS order); default all 1."""
    if not w:
        return (1.0,) * N_GROUPS
    try:
        weights = tuple(round(float(v), 2) for v in w.split(","))
    except ValueError:
        raise HTTPException(422, "w må være tall adskilt med komma") from None
    if len(weights) != N_GROUPS or not all(0 <= v <= 3 for v in weights):
        raise HTTPException(422, f"w må ha {N_GROUPS} vekter mellom 0 og 3")
    return weights


@lru_cache(maxsize=16)
def load_reference(path: Path, mtime: float) -> tuple[float, np.ndarray]:
    with np.load(path) as ref:
        return float(ref["bias"]), ref["contribs"]


@lru_cache(maxsize=256)
def reference_margins(path: Path, mtime: float, weights: Weights) -> np.ndarray:
    """Sorted weighted log-odds of the background sample: the percentile scale."""
    bias, contribs = load_reference(path, mtime)
    return np.sort(bias + contribs @ np.asarray(weights, dtype=np.float32))


def percentiles(files: ModelFiles, steps: np.ndarray, weights: Weights) -> np.ndarray:
    """Percentile (0..1) of cells given their int8 group contributions (groups first axis)."""
    mtime = files.reference.stat().st_mtime
    bias, _ = load_reference(files.reference, mtime)
    ref = reference_margins(files.reference, mtime, weights)
    w = np.asarray(weights, dtype=np.float32)
    margin = bias + CONTRIB_SCALE * np.tensordot(w, steps.astype(np.float32), axes=1)
    return np.searchsorted(ref, margin) / len(ref)


@lru_cache(maxsize=16)
def load_meta(path: Path, mtime: float) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def meta(files: ModelFiles) -> dict[str, Any]:
    return load_meta(files.meta, files.meta.stat().st_mtime)


@app.get("/api/config")
def client_config(user: MaybeUser) -> dict[str, Any]:
    """Settings the map needs: which extra layers this user can use."""
    return {
        "imagery": config.ESRI_API_KEY is not None and auth.can(user, "imagery"),
        "strava": config.STRAVA_SESSION is not None and auth.can(user, "strava"),
    }


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=config.SESSION_DAYS * 86_400,
        httponly=True,
        samesite="lax",
        secure=config.COOKIE_SECURE,
    )


@app.post("/api/auth/login")
def login(body: Login, response: Response) -> dict[str, Any]:
    if auth.is_locked_out(body.username):
        raise HTTPException(429, "For mange feil forsøk. Prøv igjen om 15 minutter.")
    result = auth.login(body.username, body.password)
    if result is None:
        raise HTTPException(401, "Feil brukernavn eller passord")
    user, token = result
    set_session_cookie(response, token)
    return user.as_dict()


class Registration(BaseModel):
    username: str = Field(min_length=2, max_length=100, pattern=r"^[\w.@-]+$")
    password: str = Field(min_length=8, max_length=200)


@app.post("/api/auth/register")
def register(body: Registration, response: Response) -> dict[str, Any]:
    """Anyone can register as a regular user, without access to anything extra
    until an admin grants it. Logs the new user in."""
    if not auth.registration_allowed():
        raise HTTPException(429, "For mange nye brukere akkurat nå. Prøv igjen senere.")
    try:
        auth.add_user(body.username, body.password, False, set())
    except ValueError as err:
        raise HTTPException(409, str(err)) from None
    result = auth.login(body.username, body.password)
    assert result is not None
    user, token = result
    set_session_cookie(response, token)
    return user.as_dict()


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.logout(token)
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: MaybeUser) -> dict[str, Any] | None:
    return user.as_dict() if user else None


def admin_user_dict(user: User) -> dict[str, Any]:
    # The permissions granted, not everything an admin can do.
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "permissions": sorted(user.permissions),
        "created_at": user.created_at,
    }


@app.get("/api/admin/users")
def admin_users(admin: Admin) -> dict[str, Any]:
    return {
        "permissions": [{"key": k, "label": v} for k, v in auth.PERMISSIONS.items()],
        "users": [admin_user_dict(u) for u in auth.list_users()],
    }


class NewUser(BaseModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[\w.@-]+$")
    password: str = Field(min_length=8, max_length=200)
    is_admin: bool = False
    permissions: list[str] = []


@app.post("/api/admin/users")
def admin_add_user(body: NewUser, admin: Admin) -> dict[str, Any]:
    try:
        user = auth.add_user(body.username, body.password, body.is_admin, set(body.permissions))
    except ValueError as err:
        raise HTTPException(409, str(err)) from None
    return admin_user_dict(user)


class UserChanges(BaseModel):
    is_admin: bool | None = None
    permissions: list[str] | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(user_id: int, body: UserChanges, admin: Admin) -> dict[str, Any]:
    if user_id == admin.id and body.is_admin is False:
        raise HTTPException(409, "Du kan ikke fjerne din egen admin-tilgang")
    user = auth.update_user(
        user_id,
        is_admin=body.is_admin,
        permissions=set(body.permissions) if body.permissions is not None else None,
        password=body.password,
    )
    if user is None:
        raise HTTPException(404, "Fant ikke brukeren")
    return admin_user_dict(user)


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: Admin) -> dict[str, bool]:
    """Deletes the user. Their findings are kept (and still used in training)."""
    if user_id == admin.id:
        raise HTTPException(409, "Du kan ikke slette deg selv")
    if not auth.delete_user(user_id):
        raise HTTPException(404, "Fant ikke brukeren")
    return {"deleted": True}


_esri_client: httpx.AsyncClient | None = None
_redis: redis.Redis | None = None
# private: only for logged-in users with access, so no shared caches.
IMAGERY_HEADERS = {"Cache-Control": "private, max-age=86400"}
# Cached "no tile here" answers, so missing tiles aren't requested again and again.
NO_TILE = b"-"


def esri_client() -> httpx.AsyncClient:
    global _esri_client
    if _esri_client is None:
        headers = {"Referer": config.ESRI_REFERER} if config.ESRI_REFERER else {}
        _esri_client = httpx.AsyncClient(timeout=20, headers=headers)
    return _esri_client


def redis_client() -> redis.Redis | None:
    global _redis
    if _redis is None and config.REDIS_URL:
        _redis = redis.Redis.from_url(config.REDIS_URL, socket_timeout=1)
    return _redis


def cache_seconds(res: httpx.Response) -> int:
    """How long the source allows a tile to be cached (its Cache-Control max-age)."""
    match = re.search(r"max-age=(\d+)", res.headers.get("cache-control", ""))
    return int(match.group(1)) if match else config.IMAGERY_CACHE_DEFAULT_S


async def cache_get(key: str) -> bytes | None:
    cache = redis_client()
    if cache is None:
        return None
    try:
        return cast(bytes | None, await cache.get(key))
    except redis.RedisError:
        return None  # a broken cache must not break the map


async def cache_set(key: str, value: bytes, seconds: int) -> None:
    cache = redis_client()
    if cache is None or seconds <= 0:
        return
    try:
        await cache.set(key, value, ex=seconds)
    except redis.RedisError:
        pass


@app.get("/api/imagery/{z}/{x}/{y}.jpg")
async def imagery(z: int, x: int, y: int, user: MaybeUser) -> Response:
    """Aerial photo tile from Esri World Imagery.

    Fetched here so the API key stays secret, and cached in Redis for as long as
    Esri allows (24 h), so the same photos aren't requested over and over.
    """
    require_permission(user, "imagery")
    if config.ESRI_API_KEY is None:
        raise HTTPException(404, "Flyfoto er ikke satt opp (ESRI_API_KEY)")
    if not (0 <= z <= 23 and 0 <= x < 2**z and 0 <= y < 2**z):
        raise HTTPException(404, "Ugyldig flis")
    key = f"imagery:{z}:{x}:{y}"
    cached = await cache_get(key)
    if cached == NO_TILE:
        return Response(status_code=204, headers={"X-Cache": "HIT"})
    if cached is not None:
        return Response(
            cached, media_type="image/jpeg", headers=IMAGERY_HEADERS | {"X-Cache": "HIT"}
        )
    res = await esri_client().get(
        config.ESRI_IMAGERY_URL.format(z=z, x=x, y=y), params={"token": config.ESRI_API_KEY}
    )
    if res.status_code == 404:
        await cache_set(key, NO_TILE, cache_seconds(res))
        return Response(status_code=204, headers={"X-Cache": "MISS"})
    if res.status_code != 200 or not res.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(502, f"Esri svarte {res.status_code}")
    await cache_set(key, res.content, cache_seconds(res))
    return Response(
        res.content,
        media_type=res.headers["content-type"],
        headers=IMAGERY_HEADERS | {"X-Cache": "MISS"},
    )


@app.get("/api/strava/{activity}/{z}/{x}/{y}.png")
async def strava_tile(activity: str, z: int, x: int, y: int, user: MaybeUser) -> Response:
    """Strava global heatmap tile, fetched with your Strava login and cached in Redis."""
    require_permission(user, "strava")
    heatmap = stravaheat.heatmap()
    if heatmap is None:
        raise HTTPException(404, "Strava heatmap er ikke satt opp (STRAVA_SESSION)")
    if activity not in stravaheat.ACTIVITIES or not (
        0 <= z <= 20 and 0 <= x < 2**z and 0 <= y < 2**z
    ):
        raise HTTPException(404, "Ugyldig flis")
    key = f"strava:{activity}:{z}:{x}:{y}"
    cached = await cache_get(key)
    if cached == NO_TILE:
        return Response(status_code=204, headers={"X-Cache": "HIT"})
    if cached is not None:
        return Response(
            cached, media_type="image/png", headers=IMAGERY_HEADERS | {"X-Cache": "HIT"}
        )
    try:
        res = await heatmap.tile(activity, z, x, y)
    except stravaheat.StravaAuthError as err:
        raise HTTPException(503, str(err)) from None
    if res.status_code != 200 or not res.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(502, f"Strava svarte {res.status_code}")
    if is_empty_strava_tile(res.content):
        await cache_set(key, NO_TILE, cache_seconds(res))
        return Response(status_code=204, headers={"X-Cache": "MISS"})
    await cache_set(key, res.content, cache_seconds(res))
    return Response(
        res.content, media_type="image/png", headers=IMAGERY_HEADERS | {"X-Cache": "MISS"}
    )


def is_empty_strava_tile(png: bytes) -> bool:
    """Strava answers tiles without activity with an opaque all-black grayscale PNG.

    Real heatmap tiles are palette PNGs with transparency, so the PNG colour type
    in the header (byte 25; 0 = grayscale without alpha) tells them apart.
    """
    return len(png) > 25 and png[:8] == b"\x89PNG\r\n\x1a\n" and png[25] == 0


TRAILS_WMS = "https://wms.geonorge.no/skwms1/wms.friluftsruter2"
TRAIL_LAYERS = {
    "Fotrute": "Fotrute",
    "Skiloype": "Skiløype",
    "Sykkelrute": "Sykkelrute",
    "AnnenRute": "Annen rute",
}
TRAIL_FIELDS = {
    "rutenavn": "Navn",
    "rutenummer": "Rutenummer",
    "vedlikeholdsansvarlig": "Vedlikeholdes av",
    "merking_d": "Merking",
    "gradering_d": "Gradering",
    "belysning": "Belysning",
    "spesialrutetype_d": "Type",
    "tilpasning_d": "Tilpasning",
    "preparering_d": "Preparering",
}
TO_MERCATOR = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_trails_client: httpx.AsyncClient | None = None


@app.get("/api/trails")
async def trails(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    tolerance_m: float = Query(20, gt=0, le=2000),
) -> list[dict[str, Any]]:
    """Kartverket's hiking/ski/cycle routes (Turrutebasen, incl. DNT/UT.no routes) near a spot."""
    global _trails_client
    if _trails_client is None:
        _trails_client = httpx.AsyncClient(timeout=15)
    x, y = TO_MERCATOR.transform(lon, lat)
    # The WMS counts features within ~3 pixels of the queried pixel, so ask for a
    # tiny 7 px image where 3 px ≈ tolerance_m, and query its middle pixel.
    size = 7
    half = tolerance_m * size / 6

    res = await _trails_client.get(
        TRAILS_WMS,
        params={
            "SERVICE": "WMS",
            "VERSION": "1.3.0",
            "REQUEST": "GetFeatureInfo",
            "LAYERS": ",".join(TRAIL_LAYERS),
            "QUERY_LAYERS": ",".join(TRAIL_LAYERS),
            "STYLES": "",
            "CRS": "EPSG:3857",
            "BBOX": f"{x - half},{y - half},{x + half},{y + half}",
            "WIDTH": str(size),
            "HEIGHT": str(size),
            "I": str(size // 2),
            "J": str(size // 2),
            "INFO_FORMAT": "application/vnd.ogc.gml",
            "FEATURE_COUNT": "5",
        },
    )
    if res.status_code != 200:
        raise HTTPException(502, f"Kartverket svarte {res.status_code}")
    routes = []
    seen = set()
    for layer in ElementTree.fromstring(res.content):
        kind = TRAIL_LAYERS.get(layer.tag.removesuffix("_layer"))
        for feature in layer:
            fields = [
                {"label": TRAIL_FIELDS[child.tag], "value": child.text.strip()}
                for child in feature
                if child.tag in TRAIL_FIELDS and child.text and child.text.strip()
                if child.text.strip() != "Ukjent"
            ]
            key = (kind, tuple((f["label"], f["value"]) for f in fields))
            if fields and key not in seen:
                seen.add(key)
                routes.append({"type": kind, "fields": fields})
    return routes


TRAILS_WFS = "https://wfs.geonorge.no/skwms1/wfs.turogfriluftsruter"
WFS_TRAIL_TYPES = {
    "Fotrute": "Fotrute",
    "Skiløype": "Skiløype",
    "Sykkelrute": "Sykkelrute",
    "AnnenRute": "Annen rute",
}
# The WFS's field names (the WMS adds _d to decoded ones), with the same labels as /api/trails.
WFS_TRAIL_FIELDS = {
    "rutenavn": "Navn",
    "rutenummer": "Rutenummer",
    "vedlikeholdsansvarlig": "Vedlikeholdes av",
    "merking": "Merking",
    "gradering": "Gradering",
    "belysning": "Belysning",
}
# The WFS gives codes where the WMS gives text; only the codes we know are shown.
WFS_CODES = {
    "merking": {"JA": "Merket", "NEI": "Umerket", "SM": "Sesongmerket"},
    "gradering": {
        "G": "Enkel (grønn)",
        "B": "Middels (blå)",
        "R": "Krevende (rød)",
        "S": "Ekspert (svart)",
    },
    "belysning": {"true": "Ja"},
}
GML = "{http://www.opengis.net/gml/3.2}"
MAX_TRAIL_FEATURES = 5000


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def trail_fields(feature: ElementTree.Element) -> list[dict[str, str]]:
    """The route's fields, with the same labels as /api/trails."""
    fields = []
    for element in feature.iter():
        name = local_name(element.tag)
        text = (element.text or "").strip()
        if not text or name not in WFS_TRAIL_FIELDS or text == "Ukjent":
            continue
        if name in WFS_CODES:
            text = WFS_CODES[name].get(text, "")
        if text:
            fields.append({"label": WFS_TRAIL_FIELDS[name], "value": text})
    return fields


@app.get("/api/trails/area")
async def trails_area(
    west: float = Query(ge=-180, le=180),
    south: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
) -> dict[str, Any]:
    """Kartverket's routes in an area as GeoJSON lines, for route info without reception."""
    global _trails_client
    if _trails_client is None:
        _trails_client = httpx.AsyncClient(timeout=15)
    out: list[dict[str, Any]] = []
    for type_name, kind in WFS_TRAIL_TYPES.items():
        res = await _trails_client.get(
            TRAILS_WFS,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": f"app:{type_name}",
                "count": str(MAX_TRAIL_FEATURES),
                # EPSG:4326 in WFS 2.0 is latitude first.
                "bbox": f"{south},{west},{north},{east},urn:ogc:def:crs:EPSG::4326",
            },
        )
        if res.status_code != 200:
            raise HTTPException(502, f"Kartverket svarte {res.status_code}")
        for member in ElementTree.fromstring(res.content):
            for feature in member:
                lines = []
                for pos_list in feature.iter(f"{GML}posList"):
                    values = [float(v) for v in (pos_list.text or "").split()]
                    # EPSG:4258 is latitude first too.
                    lines.append([[values[i + 1], values[i]] for i in range(0, len(values) - 1, 2)])
                if lines:
                    out.append(
                        {
                            "type": "Feature",
                            "geometry": {"type": "MultiLineString", "coordinates": lines},
                            "properties": {"type": kind, "fields": trail_fields(feature)},
                        }
                    )
    return {"type": "FeatureCollection", "features": out}


@app.get("/api/species")
def species_list(user: MaybeUser) -> list[dict[str, Any]]:
    """The species this user can see."""
    return [
        {"key": s.key, "name": s.name, "latin": s.latin, "ready": is_ready(model_files(s.key))}
        for s in config.SPECIES.values()
        if not s.restricted or auth.can(user, s.key)
    ]


@app.get("/api/{species}/status")
def status(species: str, user: MaybeUser) -> dict[str, Any]:
    files = get_files(species, user)
    my_findings = len(userfindings.list_for(species, user.id)) if user else 0
    extra = {"training": is_training(species), "my_findings": my_findings}
    if not is_ready(files):
        return {
            "ready": False,
            "message": "Ingen modell ennå. Kjør: docker compose run --rm backend soppkart all",
            "model": None,
            **extra,
        }
    return {"ready": True, "message": "ok", "model": meta(files), **extra}


@lru_cache(maxsize=4096)
def render_tile(
    species: str, z: int, x: int, y: int, threshold: float, weights: Weights, mtime: float
) -> bytes | None:
    files = model_files(species)
    with Reader(str(files.contrib)) as src:
        try:
            img = src.tile(x, y, z, tilesize=256)
        except TileOutsideBounds:
            return None
    valid = img.mask > 0
    if not valid.any():
        return None
    ranks = percentiles(files, np.asarray(img.data), weights)
    # 1..255 = percentile, 0 = no data (see colormap).
    values = np.where(valid, 1 + np.round(ranks * 254), 0).astype(np.uint8)
    return bytes(
        render(
            values[np.newaxis],
            mask=img.mask,
            img_format="PNG",
            colormap=colormap(threshold),
        )
    )


@app.get("/api/{species}/tiles/{z}/{x}/{y}.png")
def tile(
    species: str,
    user: MaybeUser,
    z: int,
    x: int,
    y: int,
    threshold: float = Query(DEFAULT_THRESHOLD, ge=0, le=0.99),
    w: str | None = None,
) -> Response:
    """Score tile with weights w per feature group. Scores below threshold are transparent."""
    files = get_files(species, user)
    if not is_ready(files):
        return Response(status_code=204)
    weights = parse_weights(w)
    threshold = round(threshold, 2)
    png = render_tile(species, z, x, y, threshold, weights, files.contrib.stat().st_mtime)
    if png is None:
        return Response(status_code=204, headers=cache_headers(species))
    return Response(png, media_type="image/png", headers=cache_headers(species))


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
    user: MaybeUser,
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    w: str | None = None,
) -> dict[str, Any]:
    files = get_files(species, user)
    if not is_ready(files):
        raise HTTPException(503, "Modellen er ikke trent ennå")
    weights = parse_weights(w)
    x, y = TO_GRID.transform(lon, lat)
    values = read_pixel(features.FEATURES_PATH, x, y)
    steps = read_pixel(files.contrib, x, y)
    land = read_pixel(features.LAND_PATH, x, y)
    if values is None or steps is None or land is None:
        return {"lat": lat, "lon": lon, "inside": False, "score": None, "features": []}
    in_habitat = bool(steps[0] != CONTRIB_NODATA)
    score: float | None = None
    groups: list[dict[str, Any]] = []
    if in_habitat:
        score = round(float(percentiles(files, steps, weights)), 3)
        groups = [
            {
                "key": key,
                "label": label,
                "weight": weight,
                # Weighted log-odds: > 0 pulls the score up here, < 0 pulls it down.
                "contribution": round(float(step) * CONTRIB_SCALE * weight, 2),
            }
            for (key, label, _), step, weight in zip(
                features.FEATURE_GROUPS, steps, weights, strict=True
            )
        ]
    elif land[0]:
        score = 0.0  # land, but outside the species' habitat
    return {
        "lat": lat,
        "lon": lon,
        "inside": True,
        "score": score,
        "groups": groups,
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


# Largest area for offline point data (~16 x 16 km at 32 m), and how many rows of the
# background sample to send for computing percentiles on the phone.
MAX_AREA_CELLS = 250_000
OFFLINE_REFERENCE_ROWS = 10_000


def b64(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array).tobytes()).decode()


@app.get("/api/{species}/area")
def area(
    species: str,
    user: MaybeUser,
    west: float = Query(ge=-180, le=180),
    south: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
) -> dict[str, Any]:
    """Everything /point needs for every square in an area, so the phone can show the
    score, factor groups and nature data without reception (see frontend offline.ts).

    Arrays are base64 of raw little-endian data in row-major order: steps int8
    [groups, h, w], features float16 [features, h, w], land uint8 [h, w], reference
    float32 [rows, groups] (a sample of the background's group contributions).
    """
    files = get_files(species, user)
    if not is_ready(files):
        raise HTTPException(503, "Modellen er ikke trent ennå")
    xs, ys = TO_GRID.transform([west, east, west, east], [south, south, north, north])
    with rasterio.open(features.FEATURES_PATH) as fsrc:
        res = fsrc.transform.a
        col0 = max(0, math.floor((min(xs) - fsrc.transform.c) / res))
        col1 = min(fsrc.width, math.ceil((max(xs) - fsrc.transform.c) / res))
        row0 = max(0, math.floor((fsrc.transform.f - max(ys)) / res))
        row1 = min(fsrc.height, math.ceil((fsrc.transform.f - min(ys)) / res))
        if col1 <= col0 or row1 <= row0:
            raise HTTPException(404, "Området er utenfor kartet")
        if (col1 - col0) * (row1 - row0) > MAX_AREA_CELLS:
            raise HTTPException(413, "Området er for stort for punktinfo uten nett")
        window = Window(col0, row0, col1 - col0, row1 - row0)  # type: ignore[call-arg]
        feats = fsrc.read(window=window).astype(np.float16)
        x0 = fsrc.transform.c + col0 * res
        y0 = fsrc.transform.f - row0 * res
    with rasterio.open(files.contrib) as csrc:
        steps = csrc.read(window=window)
    with rasterio.open(features.LAND_PATH) as lsrc:
        land = lsrc.read(1, window=window)
    bias, contribs = load_reference(files.reference, files.reference.stat().st_mtime)
    step = max(1, math.ceil(len(contribs) / OFFLINE_REFERENCE_ROWS))
    return {
        "species": species,
        "trained_at": meta(files)["trained_at"],
        "grid": {"x0": x0, "y0": y0, "res": res, "width": col1 - col0, "height": row1 - row0},
        "groups": [{"key": k, "label": label} for k, label, _ in features.FEATURE_GROUPS],
        "features": [{"key": f.key, "label": f.label, "unit": f.unit} for f in features.FEATURES],
        "soil_names": features.SOIL_NAMES,
        "scale": CONTRIB_SCALE,
        "nodata": CONTRIB_NODATA,
        "bias": bias,
        "steps": b64(steps.astype(np.int8)),
        "values": b64(feats.astype("<f2")),
        "land": b64(land.astype(np.uint8)),
        "reference": b64(contribs[::step].astype("<f4")),
    }


@app.get("/api/{species}/findings.geojson")
def findings(species: str, user: MaybeUser) -> FileResponse:
    files = get_files(species, user)
    if not files.findings.exists():
        raise HTTPException(404, "Ingen funn lastet ned ennå")
    return FileResponse(files.findings, media_type="application/geo+json")


class NewFinding(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    # GPS accuracy in metres; None when the spot was picked on the map.
    accuracy_m: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)
    # When it was found, for findings saved on the phone without reception and sent
    # later; None means now.
    found_at: datetime | None = None


def finding_feature(f: dict[str, Any], names: dict[int, str] | None = None) -> dict[str, Any]:
    properties = {k: f[k] for k in ("id", "accuracy_m", "found_at", "note")}
    if names is not None:
        properties["username"] = names.get(f["user_id"], "ukjent")
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [f["lon"], f["lat"]]},
        "properties": properties,
    }


@app.get("/api/{species}/my-findings")
def my_findings(
    species: str,
    user: MaybeUser,
    scope: str = Query("mine", pattern="^(mine|all)$"),
) -> dict[str, Any]:
    """Your findings, or with scope=all (admin only) everyone's, with who found them."""
    get_files(species, user)
    if user is None:
        rows, names = [], None
    elif scope == "all":
        if not user.is_admin:
            raise HTTPException(403, "Bare admin kan se alle funn")
        rows, names = userfindings.list_for(species), auth.usernames()
    else:
        rows, names = userfindings.list_for(species, user.id), None
    return {"type": "FeatureCollection", "features": [finding_feature(f, names) for f in rows]}


@app.post("/api/{species}/my-findings")
def add_my_finding(species: str, finding: NewFinding, user: LoggedIn) -> dict[str, Any]:
    get_files(species, user)
    # A spot picked on the map is where you say it is.
    accuracy = finding.accuracy_m if finding.accuracy_m is not None else 5.0
    found_at = finding.found_at
    if found_at is not None:
        found_at = found_at.astimezone(UTC) if found_at.tzinfo else found_at.replace(tzinfo=UTC)
        if found_at > datetime.now(UTC) + timedelta(hours=1):
            raise HTTPException(422, "found_at kan ikke være frem i tid")
    added = userfindings.add(
        user.id, species, finding.lat, finding.lon, accuracy, finding.note, found_at
    )
    return finding_feature(added)


@app.delete("/api/my-findings/{finding_id}")
def delete_my_finding(finding_id: int, user: LoggedIn) -> dict[str, bool]:
    """Delete one of your findings; admins can delete anyone's."""
    finding = userfindings.get(finding_id)
    if finding is None or (finding["user_id"] != user.id and not user.is_admin):
        raise HTTPException(404, "Fant ikke funnet")
    userfindings.delete(finding_id)
    return {"deleted": True}


@app.post("/api/{species}/retrain")
def retrain(species: str, user: LoggedIn) -> dict[str, bool]:
    """Retrain the species' model in the background, with trusted users' findings."""
    get_files(species, user)
    require_permission(user, "training")
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
