"""'Soppmuligheter nå': how likely the species is to be out right now at a place.

Combines where in the species' season the place is today (season.py) with the
weather: night temperatures from MET Norway's forecast, and rain over the last
two weeks measured at the nearest weather station (MET's Frost API). The
weather each species needs is in config.Conditions. Says nothing about whether
the place itself is good habitat; that is the map.
"""

import logging
import math
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from soppkart import config, season

log = logging.getLogger(__name__)

OSLO = ZoneInfo("Europe/Oslo")
FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
FROST_URL = "https://frost.met.no"
RAIN_ELEMENT = "sum(precipitation_amount P1D)"
MIN_TEMP_ELEMENT = "min(air_temperature P1D)"
FORECAST_TTL_S = 30 * 60
STATION_TTL_S = 60 * 60
# A day with at least this much rain counts as a proper rain.
RAIN_DAY_MM = 5
# Days after a proper rain that mushrooms keep coming up.
RAIN_EFFECT_DAYS = 10
# Species that fruit until the first frost: after this day of the year (mid-December)
# the season is over even without frost.
LAST_FROST_FREE_DOY = 350
# Stations further away say little about the rain here.
MAX_STATION_KM = 30

_client: httpx.AsyncClient | None = None
_cache: dict[str, tuple[float, Any]] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15, headers={"User-Agent": config.MET_USER_AGENT})
    return _client


def cached(key: str) -> Any:
    hit = _cache.get(key)
    return hit[1] if hit and hit[0] > time.time() else None


def remember(key: str, value: Any, ttl_s: float) -> None:
    if len(_cache) > 5000:
        now = time.time()
        for k in [k for k, (expires, _) in _cache.items() if expires <= now]:
            del _cache[k]
    _cache[key] = (time.time() + ttl_s, value)


# ---- Weather -------------------------------------------------------------------------


async def forecast(lat: float, lon: float) -> dict[str, Any]:
    """MET's forecast for the spot (lat/lon rounded to ~1 km): hourly entries and altitude."""
    key = f"forecast:{lat:.2f}:{lon:.2f}"
    if (hit := cached(key)) is not None:
        return dict(hit)
    res = await client().get(FORECAST_URL, params={"lat": f"{lat:.2f}", "lon": f"{lon:.2f}"})
    res.raise_for_status()
    data = res.json()
    hours = []
    for entry in data["properties"]["timeseries"]:
        details = entry["data"]["instant"]["details"]
        next1 = entry["data"].get("next_1_hours")
        if next1 is None:
            break  # further out the forecast is in 6 h steps
        hours.append(
            {
                "time": entry["time"],
                "temp_c": details.get("air_temperature"),
                "wind_ms": details.get("wind_speed"),
                "wind_from_deg": details.get("wind_from_direction"),
                "humidity_pct": details.get("relative_humidity"),
                "precip_mm": next1["details"].get("precipitation_amount", 0.0),
                "symbol": next1["summary"].get("symbol_code"),
            }
        )
    coords = data["geometry"]["coordinates"]
    result = {"hours": hours, "altitude_m": coords[2] if len(coords) > 2 else None}
    remember(key, result, FORECAST_TTL_S)
    return result


async def station_days(
    lat: float, lon: float, element: str, start: date, combine: str
) -> tuple[dict[str, Any], dict[date, float]] | None:
    """Daily values of a Frost element since start at the nearest station that has them.

    combine: how to merge several series for a day: "PT6H" prefers the 06-06 day
    (rain sums), "min" takes the lowest (temperatures). Returns (station, values
    per day), or None when no station within MAX_STATION_KM has enough data.
    """
    assert config.FROST_CLIENT_ID is not None
    auth = (config.FROST_CLIENT_ID, "")
    res = await client().get(
        f"{FROST_URL}/sources/v0.jsonld",
        params={
            "types": "SensorSystem",
            "elements": element,
            "geometry": f"nearest(POINT({lon:.4f} {lat:.4f}))",
            "nearestmaxcount": 5,
        },
        auth=auth,
    )
    if res.status_code == 404:
        return None
    res.raise_for_status()
    stations = [s for s in res.json()["data"] if s.get("distance", 0) <= MAX_STATION_KM]
    if not stations:
        return None
    today = datetime.now(OSLO).date()
    res = await client().get(
        f"{FROST_URL}/observations/v0.jsonld",
        params={
            "sources": ",".join(s["id"] for s in stations),
            "elements": element,
            "referencetime": f"{start.isoformat()}/{(today + timedelta(days=1)).isoformat()}",
        },
        auth=auth,
    )
    # 404/412: none of the stations has observations in the period.
    if res.status_code in (404, 412):
        return None
    res.raise_for_status()
    daily: dict[str, dict[date, float]] = {}
    for item in res.json()["data"]:
        days = daily.setdefault(item["sourceId"].split(":")[0], {})
        day = datetime.fromisoformat(item["referenceTime"]).date()
        for obs in item["observations"]:
            value = float(obs["value"])
            if day not in days:
                days[day] = value
            elif combine == "min":
                days[day] = min(days[day], value)
            elif obs.get("timeOffset") == combine:
                days[day] = value
    needed = 0.7 * (today - start).days
    for s in stations:  # nearest first
        days = daily.get(s["id"], {})
        if len(days) >= needed:  # not too many gaps
            station = {
                "station": s.get("shortName") or s.get("name"),
                "distance_km": round(float(s["distance"]), 1),
                "station_masl": s.get("masl"),
            }
            return station, days
    return None


async def station_rain(lat: float, lon: float) -> dict[str, Any] | None:
    """Measured daily rain over the last 14 days at the nearest station that has it.

    None when Frost isn't set up or no station within MAX_STATION_KM has data.
    """
    if config.FROST_CLIENT_ID is None:
        return None
    key = f"rain:{lat:.2f}:{lon:.2f}"
    if (hit := cached(key)) is not None:
        return hit or None
    today = datetime.now(OSLO).date()
    found = await station_days(lat, lon, RAIN_ELEMENT, today - timedelta(days=14), "PT6H")
    result = None
    if found is not None:
        station, days = found
        rain_days = [d for d, mm in days.items() if mm >= RAIN_DAY_MM]
        result = station | {
            "rain_14d_mm": round(sum(days.values()), 1),
            "rain_7d_mm": round(sum(mm for d, mm in days.items() if d > today - timedelta(7)), 1),
            "days_since_rain_day": (today - max(rain_days)).days if rain_days else None,
            "last_day": max(days).isoformat(),
        }
    remember(key, result or {}, STATION_TTL_S)
    return result


async def station_frost(
    lat: float, lon: float, since: date, frost_c: float
) -> dict[str, Any] | None:
    """The first night at or below frost_c since a date at the nearest station, if any.

    None when Frost isn't set up or no station has the daily minimum temperature.
    """
    if config.FROST_CLIENT_ID is None:
        return None
    key = f"frost:{lat:.2f}:{lon:.2f}:{since}:{frost_c}"
    if (hit := cached(key)) is not None:
        return hit or None
    found = await station_days(lat, lon, MIN_TEMP_ELEMENT, since, "min")
    result = None
    if found is not None:
        station, days = found
        frosts = sorted(d for d, t in days.items() if t <= frost_c)
        result = station | {
            "first_frost": frosts[0].isoformat() if frosts else None,
            "first_frost_c": days[frosts[0]] if frosts else None,
            "coldest_c": min(days.values()),
        }
    remember(key, result or {}, STATION_TTL_S)
    return result


# ---- Assessment ----------------------------------------------------------------------

WIND_DIRECTIONS = ["N", "NØ", "Ø", "SØ", "S", "SV", "V", "NV"]
BEAUFORT = [
    (0.3, "Stille"),
    (1.6, "Flau vind"),
    (3.4, "Svak vind"),
    (5.5, "Lett bris"),
    (8.0, "Laber bris"),
    (10.8, "Frisk bris"),
    (13.9, "Liten kuling"),
    (17.2, "Stiv kuling"),
    (20.8, "Sterk kuling"),
    (24.5, "Liten storm"),
    (28.5, "Full storm"),
    (32.7, "Sterk storm"),
]


def wind_text(speed: float | None, from_deg: float | None) -> str | None:
    if speed is None:
        return None
    name = next((n for limit, n in BEAUFORT if speed < limit), "Orkan")
    direction = "" if from_deg is None else f" {WIND_DIRECTIONS[round(from_deg / 45) % 8]}"
    return f"{speed:.1f} m/s{direction} · {name}"


def factor(status: str, text: str) -> dict[str, str]:
    """One line of the assessment; status good/ok/bad, or info (not counted)."""
    return {"status": status, "text": text}


def season_part(
    species: config.Species,
    lat: float,
    elevation_m: float | None,
    today: date,
    frost: dict[str, Any] | None,
) -> tuple[float, dict[str, str]]:
    """Where in the season today is. For species whose season ends with the first
    frost (Conditions.frost_ends_season), the late season lasts as long as the
    nearest station (frost, see station_frost) hasn't had a frost night yet."""
    data = season.load_season(species)
    if data is None:
        return 0.6, factor("info", "Ingen sesongdata ennå")
    days = season.season_days(data, lat, elevation_m or 0)
    doy = today.timetuple().tm_yday
    value = season.season_factor(days, doy)
    if doy < days["q05"]:
        wait = round(days["q05"] - doy)
        return value, factor("bad", f"Før registrert sesong (starter om ca. {wait} dager her)")
    if doy < days["q25"]:
        return value, factor("ok", "Tidlig i registrert soppsesong")
    if doy <= days["q75"]:
        return value, factor("good", "Midt i registrert soppsesong")
    c = species.conditions
    if c.frost_ends_season and frost is not None and doy <= LAST_FROST_FREE_DOY:
        where = f"målestasjon {frost['distance_km']:.1f} km unna"
        if frost["first_frost"] is None:
            return 1.0, factor(
                "good",
                f"Sent i sesongen, men ingen frost ennå (kaldeste natt {frost['coldest_c']:.1f}°C, "
                f"{where}): kommer gjerne til første frost",
            )
        first = date.fromisoformat(frost["first_frost"]).strftime("%d.%m")
        return min(value, 0.2), factor(
            "bad",
            f"Første frost var {first} ({frost['first_frost_c']:.1f}°C, {where}): "
            "sesongen er trolig over",
        )
    if doy <= days["q95"]:
        return value, factor("ok", "Sent i registrert soppsesong")
    return value, factor("bad", "Etter registrert sesong her")


def night_part(c: config.Conditions, hours: list[dict[str, Any]]) -> tuple[float, dict[str, str]]:
    """The coming (or current) night's lowest temperature, 21–08 local time."""

    def local_hour(h: dict[str, Any]) -> int:
        return datetime.fromisoformat(h["time"]).astimezone(OSLO).hour

    night = [
        h["temp_c"] for h in hours[:24] if h["temp_c"] is not None and not 8 < local_hour(h) < 21
    ]
    if not night:
        return 0.7, factor("info", "Ingen nattemperatur i varselet")
    t = min(night)
    lo, hi = c.night_ideal_c
    ok_lo, ok_hi = c.night_ok_c
    if lo <= t <= hi:
        return 1.0, factor("good", f"Passe netter ({t:.1f}°C)")
    if ok_lo <= t <= ok_hi:
        word = "kalde" if t < lo else "varme"
        return 0.6, factor("ok", f"Litt {word} netter ({t:.1f}°C)")
    word = "kalde" if t < ok_lo else "varme"
    return 0.25, factor("bad", f"For {word} netter ({t:.1f}°C)")


def rain_parts(c: config.Conditions, rain: dict[str, Any]) -> list[tuple[float, dict[str, str]]]:
    r14, r7 = rain["rain_14d_mm"], rain["rain_7d_mm"]
    where = f"målestasjon {rain['distance_km']:.1f} km unna"
    text = f"{r14:.1f} mm regn siste 14 dager ({r7:.0f} mm siste 7) · {where}"
    lo, good = c.rain_14d_min_mm, c.rain_14d_good_mm
    if r14 >= good:
        amount = (1.0, factor("good", text))
    elif r14 >= lo:
        amount = (0.5 + 0.5 * (r14 - lo) / (good - lo), factor("ok", text))
    else:
        amount = (0.2 + 0.3 * r14 / lo, factor("bad", f"Tørt: {text}"))
    days = rain["days_since_rain_day"]
    if days is None:
        recent = (
            0.6,
            factor("bad", f"Ingen dag med minst {RAIN_DAY_MM} mm regn de siste 14 dagene"),
        )
    elif days <= RAIN_EFFECT_DAYS:
        span = "det siste døgnet" if days <= 1 else f"de siste {days} døgnene"
        recent = (1.0, factor("good", f"Minst {RAIN_DAY_MM} mm regn i løpet av {span}"))
    else:
        recent = (
            0.8,
            factor("ok", f"Siste dag med minst {RAIN_DAY_MM} mm regn var for {days} dager siden"),
        )
    return [amount, recent]


def hiking_text(hours: list[dict[str, Any]]) -> str:
    six = hours[:6]
    rain = sum(h["precip_mm"] or 0 for h in six)
    wind = max((h["wind_ms"] or 0 for h in six), default=0)
    if rain < 0.5 and wind < 8:
        return "Godt turvær"
    if rain < 3 and wind < 12:
        return "Greit turvær"
    return "Dårlig turvær"


def label(score: int, season_value: float) -> str:
    if season_value < 0.1:
        return "Utenfor sesong"
    if score >= 60:
        return "Gode soppmuligheter"
    if score >= 35:
        return "Lovende soppmuligheter"
    if score >= 15:
        return "Middels soppmuligheter"
    return "Dårlige soppmuligheter"


async def assess(species: config.Species, lat: float, lon: float) -> dict[str, Any]:
    """The assessment for the spot today. Raises httpx errors if MET's forecast fails."""
    fc = await forecast(lat, lon)
    hours = fc["hours"]
    try:
        rain = await station_rain(lat, lon)
        rain_error = None
    except httpx.HTTPError as err:
        log.warning("Frost: %s", err)
        rain, rain_error = None, "Målt nedbør er ikke tilgjengelig akkurat nå"
    today = datetime.now(OSLO).date()
    c = species.conditions

    frost = None
    if c.frost_ends_season:
        try:
            # Frost from the start of the season counts; a summer frost night up high doesn't.
            since = date(today.year, 8, 1)
            frost = await station_frost(lat, lon, since, c.frost_c)
        except httpx.HTTPError as err:
            log.warning("Frost: %s", err)
    season_value, season_line = season_part(species, lat, fc["altitude_m"], today, frost)
    weather = [night_part(c, hours)]
    coming = sum(h["precip_mm"] or 0 for h in hours[:24])
    if coming >= 10:
        coming_line = factor("info", f"Mye regn ventes ({coming:.1f} mm neste døgn)")
    elif coming >= 1:
        coming_line = factor("info", f"Noe regn ventes ({coming:.1f} mm neste døgn)")
    else:
        coming_line = factor("info", "Lite eller ikke noe regn ventes neste døgn")
    if rain is not None:
        weather += rain_parts(c, rain)
    else:
        missing = rain_error or (
            "Målt nedbør er ikke satt opp (FROST_CLIENT_ID)"
            if config.FROST_CLIENT_ID is None
            else f"Ingen målestasjon med nedbør innen {MAX_STATION_KM} km"
        )
        weather.append((0.7, factor("info", missing)))

    # Season times the geometric mean of the weather factors.
    weather_value = math.prod(v for v, _ in weather) ** (1 / len(weather))
    score = round(100 * season_value * weather_value)
    now = hours[0] if hours else {}
    return {
        "lat": round(lat, 2),
        "lon": round(lon, 2),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "score": score,
        "label": label(score, season_value),
        "current": {
            "temp_c": now.get("temp_c"),
            "symbol": now.get("symbol"),
            "wind": wind_text(now.get("wind_ms"), now.get("wind_from_deg")),
            "humidity_pct": now.get("humidity_pct"),
            "hiking": hiking_text(hours),
        },
        "next_hours": [
            {k: h[k] for k in ("time", "temp_c", "wind_ms", "precip_mm", "symbol")}
            for h in hours[:6]
        ],
        "factors": [
            season_line,
            *(f for _, f in weather[:1]),
            coming_line,
            *(f for _, f in weather[1:]),
        ],
        "station": rain,
        "note": "Vurderingen bygger på registrert sesong og værdata"
        + (", inkludert målt nedbør" if rain else "")
        + " – ikke bekreftede funn.",
    }
