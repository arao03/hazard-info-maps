"""HTTP access with an honest User-Agent, retries, polite pacing and an on-disk response cache."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import time
from typing import Optional

import requests

from . import __version__
from .config import IMG_H, IMG_W, OVERPASS_ENDPOINTS

log = logging.getLogger("hazardmaps")

# Identify yourself to the services you query (set HAZARDMAPS_CONTACT to a URL or e-mail).
DEFAULT_CONTACT = "https://github.com/arao03/hazard-info-maps"


def default_user_agent() -> str:
    return f"hazard-info-maps/{__version__} (+{os.environ.get('HAZARDMAPS_CONTACT', DEFAULT_CONTACT)})"


class Response:
    """Minimal response object, identical for live and cached replies."""

    def __init__(self, content: bytes, headers: dict, status_code: int = 200):
        self.content, self.headers, self.status_code = content, headers, status_code

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


class HttpClient:
    def __init__(self, cache_dir: Optional[str] = None, user_agent: Optional[str] = None,
                 retries: int = 3, backoff: float = 2.0, session: Optional[requests.Session] = None):
        self.cache_dir = cache_dir
        self.retries, self.backoff = retries, backoff
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent or default_user_agent(),
                                     "Accept": "application/json, image/png, */*"})
        self.failures: list[str] = []          # labels of requests that ultimately failed
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # -- cache -----------------------------------------------------------------------------
    @staticmethod
    def _key(url: str, params: Optional[dict]) -> str:
        blob = url + "?" + json.dumps(params or {}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _cache_read(self, key: str) -> Optional[Response]:
        if not self.cache_dir:
            return None
        body, meta = (os.path.join(self.cache_dir, key + s) for s in (".bin", ".json"))
        if os.path.exists(body) and os.path.exists(meta):
            with open(body, "rb") as fh, open(meta) as fm:
                return Response(fh.read(), json.load(fm))
        return None

    def _cache_write(self, key: str, r: requests.Response) -> None:
        if not self.cache_dir:
            return
        with open(os.path.join(self.cache_dir, key + ".bin"), "wb") as fh:
            fh.write(r.content)
        with open(os.path.join(self.cache_dir, key + ".json"), "w") as fm:
            json.dump({"content-type": r.headers.get("content-type", ""), "url": r.url}, fm)

    # -- request ---------------------------------------------------------------------------
    def get(self, url: str, params: Optional[dict] = None, timeout: int = 45,
            label: str = "") -> Optional[Response]:
        key = self._key(url, params)
        cached = self._cache_read(key)
        if cached is not None:
            log.debug("[%s] cache hit", label)
            return cached
        for attempt in range(1, self.retries + 1):
            wait = self.backoff * attempt
            try:
                r = self.session.get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                log.warning("[%s] attempt %d failed: %s", label, attempt, exc)
            else:
                if r.status_code == 200:
                    self._cache_write(key, r)
                    return Response(r.content, dict(r.headers))
                log.warning("[%s] HTTP %s (attempt %d)", label, r.status_code, attempt)
                if r.status_code in (429, 502, 503, 504):
                    try:
                        wait = max(wait, float(r.headers.get("Retry-After", 0)))
                    except ValueError:
                        pass
                else:
                    break                      # client errors will not improve on retry
            if attempt < self.retries:
                time.sleep(wait)
        self.failures.append(label)
        return None


class Sources:
    """Typed fetch helpers returning GeoDataFrames / images (or None on failure)."""

    def __init__(self, client: HttpClient, overpass_urls: Optional[list] = None,
                 overpass_delay: float = 1.0):
        self.client = client
        self.overpass_urls = overpass_urls or list(OVERPASS_ENDPOINTS)
        self.overpass_delay = overpass_delay

    def tile(self, url: str, params: dict, label: str):
        from PIL import Image
        r = self.client.get(url, params, timeout=40, label=label)
        if r is None or not r.headers.get("content-type", "").startswith("image"):
            return None
        try:
            return Image.open(io.BytesIO(r.content)).convert("RGBA")
        except OSError as exc:
            log.warning("[%s] bad image: %s", label, exc)
            return None

    def arcgis(self, url: str, label: str, bbox_str: str, bbox_geom, where: str = "1=1"):
        import geopandas as gpd
        params = {"geometry": bbox_str, "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                  "spatialRel": "esriSpatialRelIntersects", "where": where, "outFields": "*",
                  "outSR": "4326", "f": "geojson", "resultRecordCount": "2000"}
        r = self.client.get(url, params, label=label)
        if r is None:
            return None
        text = r.text
        if '"error"' in text[:200] and '"features"' not in text[:300]:
            log.warning("[%s] API error: %s", label, text[:100])
            self.client.failures.append(label)
            return None
        try:
            gdf = gpd.read_file(io.StringIO(text))
        except Exception as exc:               # malformed GeoJSON from a remote service
            log.warning("[%s] parse error: %s", label, exc)
            self.client.failures.append(label)
            return None
        gdf = gdf[gdf.geometry.notna() & gdf.geometry.intersects(bbox_geom)]
        log.info("[%s] %d features", label, len(gdf))
        return gdf if not gdf.empty else None

    def overpass(self, query: str, label: str):
        import geopandas as gpd
        from shapely.geometry import LineString, Point, Polygon
        r = None
        n_before = len(self.client.failures)
        for ep in self.overpass_urls:
            time.sleep(self.overpass_delay)    # be polite to public Overpass instances
            r = self.client.get(ep, {"data": query}, timeout=90, label=f"{label}")
            if r is not None:
                break
        if r is None:
            return None
        del self.client.failures[n_before:]    # an endpoint failed but a fallback answered: not a failure
        try:
            elements = r.json().get("elements", [])
        except ValueError as exc:
            log.warning("[%s] bad JSON: %s", label, exc)
            return None
        rows = []
        for el in elements:
            t = el.get("type")
            if t == "node" and "lon" in el:
                geom = Point(el["lon"], el["lat"])
            elif t == "way" and "geometry" in el:
                coords = [(c["lon"], c["lat"]) for c in el["geometry"]]
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    geom = Polygon(coords)
                elif len(coords) >= 2:
                    geom = LineString(coords)
                else:
                    continue
            else:
                continue
            rows.append({**el.get("tags", {}), "osm_id": el["id"], "geometry": geom})
        if not rows:
            log.info("[%s] 0 elements", label)
            return None
        gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
        log.info("[%s] %d features", label, len(gdf))
        return gdf[gdf.geometry.notna()]
