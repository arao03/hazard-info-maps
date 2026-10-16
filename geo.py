"""Bounding boxes, the shared output grid, rasterisation and PNG helpers."""
from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

from .config import (NPY_PAD, NPY_TARGET_PIXELS, CENSUS_GEOCODER, ZONE_COLORS, ZONE_RISK)

log = logging.getLogger("hazardmaps")
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON_EQ = 111_320.0

_county_cache: dict = {}


def tif_wgs84_bbox(tif_path: str) -> dict:
    """Return {'xmin','ymin','xmax','ymax'} (WGS84) of a GeoTIFF."""
    import rasterio
    from rasterio.warp import transform_bounds
    with rasterio.open(tif_path) as ds:
        b = ds.bounds
        xmin, ymin, xmax, ymax = transform_bounds(ds.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top)
    return dict(xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax)


def detect_county(client, lat: float, lon: float) -> tuple:
    """(state_fips, county_fips, censusreporter_geo_id, county_name) via the Census Geocoder.

    Returns Nones if the point is outside the US or the service is unavailable; the flood-independent
    layers still run, but population needs a US county.
    """
    r = client.get(CENSUS_GEOCODER,
                   params={"x": str(lon), "y": str(lat), "benchmark": "Public_AR_Current",
                           "vintage": "Current_Current", "format": "json"},
                   timeout=15, label="Census Geocoder")
    if r is None:
        return None, None, None, "Unknown"
    try:
        county = r.json()["result"]["geographies"]["Counties"][0]
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("Census geocoder returned no county (outside the US?): %s", exc)
        return None, None, None, "Unknown"
    sf, cf = county["STATE"], county["COUNTY"]
    return sf, cf, f"05000US{sf}{cf}", county["NAME"]


def make_ctx(bbox: dict, client=None, county: Optional[tuple] = None, pad: float = NPY_PAD,
             target_pixels: int = NPY_TARGET_PIXELS, pixel_size_m: Optional[float] = None) -> dict:
    """Derive every bbox-dependent value used by the layers.

    Output grid: bbox padded by ``pad`` degrees. Size is either ``pixel_size_m`` (metres per pixel,
    preferred for reproducibility) or about ``target_pixels`` total pixels. Pixels are close to square
    on the ground. Pass ``county`` to avoid the network lookup (e.g. in tests or outside the US).
    """
    from shapely.geometry import box as shapely_box
    import geopandas as gpd

    cx, cy = (bbox["xmin"] + bbox["xmax"]) / 2, (bbox["ymin"] + bbox["ymax"]) / 2
    if county is None:
        key = (round(cy, 2), round(cx, 2))
        if key not in _county_cache:
            _county_cache[key] = detect_county(client, cy, cx) if client is not None else (None, None, None, "Unknown")
        county = _county_cache[key]
    state_fips, county_fips, cr_geo, county_name = county

    x0, x1, y0, y1 = bbox["xmin"] - pad, bbox["xmax"] + pad, bbox["ymin"] - pad, bbox["ymax"] + pad
    cos_lat = math.cos(math.radians(cy))
    w_m = (x1 - x0) * M_PER_DEG_LON_EQ * cos_lat
    h_m = (y1 - y0) * M_PER_DEG_LAT
    if pixel_size_m:
        npy_w, npy_h = max(64, round(w_m / pixel_size_m)), max(64, round(h_m / pixel_size_m))
    else:
        # Same aspect-ratio rule as the original research scripts (degrees, lon scaled by cos(lat)),
        # so default grids are identical to previously generated maps.
        aspect = (y1 - y0) / ((x1 - x0) * cos_lat)           # >1 portrait, <1 landscape
        npy_w = max(64, round(math.sqrt(target_pixels / aspect)))
        npy_h = max(64, round(npy_w * aspect))
    geom = shapely_box(bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"])
    return {
        **bbox,
        "bbox_str": f"{bbox['xmin']},{bbox['ymin']},{bbox['xmax']},{bbox['ymax']}",
        "bbox_geom": geom,
        "bbox_gdf": gpd.GeoDataFrame(geometry=[geom], crs="EPSG:4326"),
        "ovp_exact": f"{bbox['ymin']},{bbox['xmin']},{bbox['ymax']},{bbox['xmax']}",
        "cx": cx, "cy": cy,
        "state_fips": state_fips, "county_fips": county_fips,
        "cr_county_geo": cr_geo, "county_name": county_name,
        "npy_xmin": x0, "npy_xmax": x1, "npy_ymin": y0, "npy_ymax": y1,
        "npy_w": npy_w, "npy_h": npy_h, "pad": pad,
        "px_deg": (x1 - x0) / npy_w,
        "m_per_px": 0.5 * (w_m / npy_w + h_m / npy_h),
        "deg_per_m": 1.0 / (M_PER_DEG_LON_EQ * cos_lat),   # lon-degrees per metre (buffers)
    }


# --- rasterisation ------------------------------------------------------------------------------
def npy_transform(ctx):
    from rasterio.transform import from_bounds
    return from_bounds(ctx["npy_xmin"], ctx["npy_ymin"], ctx["npy_xmax"], ctx["npy_ymax"],
                       ctx["npy_w"], ctx["npy_h"])


def zeros(ctx) -> np.ndarray:
    return np.zeros((ctx["npy_h"], ctx["npy_w"]), dtype=np.float32)


def rasterize_shapes(shapes, ctx, fill: float = 0.0) -> np.ndarray:
    """Burn (geometry, value) pairs onto the shared grid (float32, north-up, later shapes win)."""
    from rasterio.features import rasterize
    h, w = ctx["npy_h"], ctx["npy_w"]
    if not shapes:
        return np.full((h, w), fill, dtype=np.float32)
    return rasterize(shapes, out_shape=(h, w), transform=npy_transform(ctx), fill=fill,
                     dtype="float32", all_touched=True)


def save_npy(arr: np.ndarray, path: str) -> None:
    np.save(path, arr.astype(np.float32))
    log.info("wrote %s shape=%s range=[%.3f, %.3f]", path, arr.shape, float(arr.min()), float(arr.max()))


# --- FEMA zone helpers ----------------------------------------------------------------------------
def zone_risk(z) -> float:
    if pd.isna(z):
        return 0.0
    z = str(z).strip().upper()
    for k, v in ZONE_RISK.items():
        if z.startswith(k):
            return v
    return 0.0


def zone_color(z) -> str:
    if pd.isna(z):
        return "#ffffbf"
    z = str(z).strip().upper()
    for k, c in ZONE_COLORS.items():
        if z.startswith(k):
            return c
    return "#ffffbf"


# --- plotting ---------------------------------------------------------------------------------------
def add_basemap(ax, enabled: bool, alpha: float = 0.4) -> None:
    """Optional OSM raster basemap. Off by default: public tile servers forbid bulk downloading."""
    if not enabled:
        return
    try:
        import contextily as cx
        cx.add_basemap(ax, crs="EPSG:4326", source=cx.providers.OpenStreetMap.Mapnik, alpha=alpha, zorder=0)
    except Exception as exc:              # network / optional dependency problems must not kill a run
        log.warning("basemap skipped: %s", exc)


def finish(ax, fig, ctx, title, fname, save: bool, dark: bool = False, source: Optional[str] = None) -> None:
    import matplotlib.pyplot as plt
    if save:
        bc = "white" if dark else "black"
        ctx["bbox_gdf"].boundary.plot(ax=ax, color=bc, linewidth=1.8, linestyle="--", zorder=25)
        ax.set_xlim(ctx["xmin"] - ctx["pad"], ctx["xmax"] + ctx["pad"])
        ax.set_ylim(ctx["ymin"] - ctx["pad"], ctx["ymax"] + ctx["pad"])
        ax.set_xlabel("Longitude", fontsize=9, color=bc)
        ax.set_ylabel("Latitude", fontsize=9, color=bc)
        if dark:
            ax.tick_params(colors="white")
            for sp in ax.spines.values():
                sp.set_edgecolor("white")
        ax.grid(True, linestyle=":", alpha=0.3)
        ax.set_title(title, fontsize=12, fontweight="bold", color=bc, pad=8)
        if source:
            ax.text(0.01, 0.01, f"Source: {source}", transform=ax.transAxes, fontsize=7,
                    color="grey", zorder=30)
        fig.tight_layout()
        kw = dict(dpi=150, bbox_inches="tight")
        if dark:
            kw["facecolor"] = fig.get_facecolor()
        fig.savefig(fname, **kw)
        log.info("wrote %s", fname)
    plt.close(fig)
