"""Strava global heatmap tiles, fetched with your own Strava login.

Strava serves the detailed heatmap only to logged-in users, through CloudFront
cookies that expire after 24 hours. Visiting the heatmap page with a valid
session cookie (_strava4_session) hands out fresh ones, so the backend renews
them itself; the session cookie only has to be replaced if Strava logs you out.
"""

import logging
import time

import httpx

from soppkart import config

log = logging.getLogger(__name__)

AUTH_URL = "https://www.strava.com/maps/global-heatmap"
TILE_URL = "https://content-a.strava.com/identified/globalheat/{activity}/hot/{z}/{x}/{y}.png"
ACTIVITIES = {"all", "run", "ride", "winter", "water"}
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:156.0) Gecko/20100101 Firefox/156.0"
CLOUDFRONT_COOKIES = ("CloudFront-Key-Pair-Id", "CloudFront-Policy", "CloudFront-Signature")
# Renew this long before Strava's cookies expire.
RENEW_MARGIN_S = 3600


class StravaAuthError(Exception):
    """Strava didn't hand out heatmap cookies: the session cookie is invalid or expired."""


class StravaHeatmap:
    def __init__(self, session: str) -> None:
        self.session = session
        self.cookies: dict[str, str] = {}
        self.expires_at = 0.0
        self.client = httpx.AsyncClient(
            timeout=20,
            headers={
                "User-Agent": USER_AGENT,
                "Origin": "https://www.strava.com",
                "Referer": "https://www.strava.com/",
            },
        )

    async def renew(self) -> None:
        res = await self.client.get(AUTH_URL, cookies={"_strava4_session": self.session})
        fresh = {name: res.cookies[name] for name in CLOUDFRONT_COOKIES if res.cookies.get(name)}
        if len(fresh) < len(CLOUDFRONT_COOKIES):
            raise StravaAuthError("Strava ga ingen heatmap-tilgang: STRAVA_SESSION er ugyldig")
        self.cookies = fresh
        # Strava tells when they expire (milliseconds); assume 24 h otherwise.
        expires_ms = res.cookies.get("_strava_CloudFront-Expires")
        self.expires_at = int(expires_ms) / 1000 if expires_ms else time.time() + 86_400
        # Strava may rotate the session cookie; keep using the newest one.
        if res.cookies.get("_strava4_session"):
            self.session = res.cookies["_strava4_session"]
        log.info("renewed Strava heatmap cookies, valid for %.1f h", self.hours_left())

    def hours_left(self) -> float:
        return (self.expires_at - time.time()) / 3600

    async def tile(self, activity: str, z: int, x: int, y: int) -> httpx.Response:
        """A heatmap tile, renewing the cookies first if they are (nearly) expired."""
        if time.time() > self.expires_at - RENEW_MARGIN_S:
            await self.renew()
        url = TILE_URL.format(activity=activity, z=z, x=x, y=y)
        params = {"v": "20", "missing": "empty"}
        res = await self.client.get(url, params=params, cookies=self.cookies)
        if res.status_code == 403:
            # Cookies rejected early (e.g. revoked): renew once and retry.
            await self.renew()
            res = await self.client.get(url, params=params, cookies=self.cookies)
        return res


_heatmap: StravaHeatmap | None = None


def heatmap() -> StravaHeatmap | None:
    """The shared heatmap client, or None when STRAVA_SESSION isn't set."""
    global _heatmap
    if _heatmap is None and config.STRAVA_SESSION:
        _heatmap = StravaHeatmap(config.STRAVA_SESSION)
    return _heatmap
