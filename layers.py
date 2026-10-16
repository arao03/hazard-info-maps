"""The five layer builders. Each writes ``<name>.npy`` (and optionally ``<name>.png``) to ``out_dir``."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import config as C
from .geo import (add_basemap, finish, rasterize_shapes, save_npy, zeros, zone_color, zone_risk)
from .net import Sources

log = logging.getLogger("hazardmaps")


@dataclass
class Options:
    png: bool = True          # also write a PNG preview of each layer
    basemap: bool = False     # draw an OSM raster basemap under the PNG (requires contextily + network)


def _not_available(ax, text, color="grey", size=10):
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", fontsize=size, color=color)


# =============================================================================================
# 1. FEMA flood hazard zones (NFHL layer 28)
# =============================================================================================
def _fema_tile_layers() -> str:
    def sym(color, outline):
        return {"type": "esriSFS", "style": "esriSFSSolid", "color": color,
                "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": outline, "width": 0.3}}
    return json.dumps([{
        "id": 28, "source": {"type": "mapLayer", "mapLayerId": 28},
        "drawingInfo": {"renderer": {
            "type": "uniqueValue", "field1": "FLD_ZONE",
            "defaultSymbol": sym([255, 255, 191, 100], [128, 128, 128, 60]),
            "uniqueValueInfos": [{"value": z, "label": z, "symbol": sym(c + [200], [60, 60, 60, 80])}
                                 for z, c in C.FEMA_TILE_COLORS]}}}])


def flood(ctx, out_dir, src: Sources, opts: Options):
    log.info("[1/5] FEMA flood hazard zones")
    fig, ax = plt.subplots(figsize=(10, 8))
    add_basemap(ax, opts.basemap)
    vec = src.arcgis(f"{C.FEMA_NFHL_MAPSERVER}/28/query", "FEMA NFHL flood zones",
                     ctx["bbox_str"], ctx["bbox_geom"], where="FLD_ZONE IS NOT NULL")
    arr = zeros(ctx)
    if vec is not None and not vec.empty and "FLD_ZONE" in vec.columns:
        vec["_c"] = vec["FLD_ZONE"].apply(zone_color)
        vec.plot(ax=ax, color=vec["_c"], edgecolor="#333333", linewidth=0.25, alpha=0.82, zorder=5)
        seen = sorted(vec["FLD_ZONE"].dropna().unique())
        ax.legend(handles=[mpatches.Patch(color=zone_color(z), label=f"Zone {z}") for z in seen],
                  loc="lower left", fontsize=8, title="FIRM Zone", framealpha=0.9)
        arr = rasterize_shapes([(g.__geo_interface__, zone_risk(z))
                                for g, z in zip(vec.geometry, vec["FLD_ZONE"]) if g is not None], ctx)
    elif opts.png:                                   # label-free image tile, picture only
        params = {"bbox": ctx["bbox_str"], "bboxSR": "4326", "imageSR": "4326",
                  "size": f"{C.IMG_W},{C.IMG_H}", "format": "png32", "transparent": "true",
                  "layers": "show:28", "dynamicLayers": _fema_tile_layers(), "dpi": "96", "f": "image"}
        img = src.tile(f"{C.FEMA_NFHL_MAPSERVER}/export", params, "FEMA dynamicLayers tile")
        if img is not None and np.array(img)[:, :, 3].max() > 10:
            ax.imshow(img, extent=[ctx["xmin"], ctx["xmax"], ctx["ymin"], ctx["ymax"]], origin="upper",
                      zorder=5, interpolation="bilinear")
        else:
            _not_available(ax, "FEMA NFHL unavailable")
    else:
        src.client.failures.append("FEMA NFHL flood zones (no vector data)")
    finish(ax, fig, ctx, "FEMA Flood Hazard Zones", os.path.join(out_dir, C.LAYER_FILES["flood"] + ".png"),
           opts.png, source="FEMA National Flood Hazard Layer (NFHL)  |  hazards.fema.gov")
    save_npy(arr, os.path.join(out_dir, C.LAYER_FILES["flood"] + ".npy"))


# =============================================================================================
# 2. Population density (ACS 5-year via CensusReporter + TIGERweb tract polygons)
# =============================================================================================
def _tract_populations(src: Sources, cr_geo: str) -> dict:
    r = src.client.get(C.CENSUS_REPORTER, {"table_ids": "B01003", "geo_ids": f"140|{cr_geo}"},
                       timeout=30, label="CensusReporter B01003")
    if r is None:
        return {}
    pop = {}
    try:
        for full_id, tables in r.json()["data"].items():
            est = tables.get("B01003", {}).get("estimate", {}).get("B01003001")
            if est is not None:
                pop[full_id.replace("14000US", "")] = int(est)
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("CensusReporter parse error: %s", exc)
        return {}
    log.info("%d tracts with population data", len(pop))
    return pop


def population(ctx, out_dir, src: Sources, opts: Options):
    import geopandas as gpd
    import io
    log.info("[2/5] Population density (Census tracts)")
    fig, ax = plt.subplots(figsize=(10, 8))
    add_basemap(ax, opts.basemap)
    arr = zeros(ctx)
    gdf = None
    if ctx["county_fips"] is None:
        log.warning("no US county found for this bbox: population layer left empty")
        src.client.failures.append("population (no US county)")
    else:
        pop = _tract_populations(src, ctx["cr_county_geo"])
        r = src.client.get(C.TIGERWEB_TRACTS,
                           {"where": f"COUNTY='{ctx['county_fips']}' AND STATE='{ctx['state_fips']}'",
                            "outFields": "GEOID,AREALAND", "outSR": "4326", "f": "geojson"},
                           timeout=30, label="TIGERweb tracts")
        if r is not None:
            try:
                gdf = gpd.read_file(io.StringIO(r.text))
                gdf = gdf[gdf.geometry.notna()]
            except Exception as exc:
                log.warning("TIGERweb parse error: %s", exc)
                gdf = None
    if gdf is not None and not gdf.empty:
        gdf["GEOID"] = gdf["GEOID"].astype(str)
        gdf["_pop"] = gdf["GEOID"].map(pop).fillna(0)
        gdf["_area_km2"] = pd.to_numeric(gdf["AREALAND"], errors="coerce").fillna(0) / 1e6
        gdf["_dens"] = (gdf["_pop"] / gdf["_area_km2"].clip(lower=0.01)).clip(upper=50_000)
        try:
            clipped = gpd.clip(gdf, ctx["bbox_geom"])
        except Exception as exc:
            log.warning("clip failed (%s); plotting unclipped tracts", exc)
            clipped = gdf
        uninh, inh = clipped[clipped["_dens"] == 0], clipped[clipped["_dens"] > 0]
        if not uninh.empty:
            uninh.plot(ax=ax, color="#e0e0e0", edgecolor="#aaaaaa", linewidth=0.4, alpha=0.7, zorder=3)
        if not inh.empty:
            inh.plot(ax=ax, column="_dens", cmap="YlOrRd", edgecolor="grey", linewidth=0.4, alpha=0.85,
                     zorder=4, legend=True, legend_kwds={"label": "People / km²", "shrink": 0.55})
        arr = rasterize_shapes([(g.__geo_interface__, float(d))
                                for g, d in zip(gdf.geometry, gdf["_dens"]) if g is not None], ctx)
    else:
        _not_available(ax, "TIGERweb geometry unavailable", size=11)
    finish(ax, fig, ctx, "Population Density — ACS 5-Year Estimates",
           os.path.join(out_dir, C.LAYER_FILES["population"] + ".png"), opts.png,
           source="CensusReporter.org (B01003) + TIGERweb Tracts_Blocks")
    save_npy(arr, os.path.join(out_dir, C.LAYER_FILES["population"] + ".npy"))   # people / km^2, unnormalised


# =============================================================================================
# 3. Critical infrastructure (USGS National Map structures + OSM; CISA sectors)
# =============================================================================================
def _usgs_points(src: Sources, layer_id: int, ctx, label: str) -> list:
    params = {"geometry": json.dumps({k: ctx[k] for k in ("xmin", "ymin", "xmax", "ymax")}),
              "geometryType": "esriGeometryEnvelope", "inSR": "4326",
              "spatialRel": "esriSpatialRelIntersects", "outFields": "NAME,FTYPE", "outSR": "4326",
              "f": "json", "resultRecordCount": 500}
    r = src.client.get(f"{C.USGS_STRUCTURES}/{layer_id}/query", params, timeout=20, label=label)
    if r is None:
        return []
    try:
        data = r.json()
    except ValueError:
        return []
    if "error" in data:
        return []
    return [{"x": f["geometry"]["x"], "y": f["geometry"]["y"],
             "name": f.get("attributes", {}).get("NAME", "")}
            for f in data.get("features", []) if "x" in f.get("geometry", {})]


_OSM_INFRA_QUERY = """
[out:json][timeout:60];
(
  node["amenity"~"^(hospital|clinic|doctors|fire_station|police|ambulance_station|coast_guard|rescue_station|pharmacy|nursing_home|shelter|government|townhall|school|university|fuel)$"]({b});
  node["emergency"]({b});
  node["power"~"^(plant|substation|generator)$"]({b});
  way["power"~"^(plant|substation|generator)$"]({b});
  node["man_made"~"^(water_tower|pumping_station|wastewater_plant|water_works)$"]({b});
  node["aeroway"~"^(aerodrome|helipad)$"]({b});
  way["aeroway"~"^(aerodrome|helipad)$"]({b});
  node["telecom"~"^(exchange|data_center)$"]({b});
  node["building"~"^(hospital|fire_station|public|government|school|university|college)$"]({b});
  way["building"~"^(hospital|fire_station|public|government|school|university|college)$"]({b});
);
out body geom;
"""


def infrastructure(ctx, out_dir, src: Sources, opts: Options):
    from shapely.geometry import Point
    log.info("[3/5] Critical infrastructure (USGS + OSM)")
    fig, ax = plt.subplots(figsize=(10, 8))
    add_basemap(ax, opts.basemap)
    handles, shapes = [], []
    buf = C.INFRA_POINT_BUFFER_M * ctx["deg_per_m"]
    total = 0
    for layer_id, (label, color, marker, size) in C.USGS_INFRA_LAYERS.items():
        pts = _usgs_points(src, layer_id, ctx, f"USGS {label}")
        if not pts:
            continue
        total += len(pts)
        ax.scatter([p["x"] for p in pts], [p["y"] for p in pts], c=color, marker=marker, s=size,
                   alpha=0.9, zorder=8, edgecolors="white", linewidths=0.5)
        handles.append(mpatches.Patch(color=color, label=f"{label} ({len(pts)})"))
        for p in pts:
            shapes.append((Point(p["x"], p["y"]).buffer(buf).__geo_interface__, 1.0))
            if p["name"] and opts.png:
                ax.annotate(p["name"], (p["x"], p["y"]), textcoords="offset points", xytext=(4, 3),
                            fontsize=6, color="#111111", zorder=15,
                            bbox=dict(boxstyle="round,pad=0.15", facecolor="white", alpha=0.65, linewidth=0))
    log.info("USGS total: %d features", total)
    osm = src.overpass(_OSM_INFRA_QUERY.format(b=ctx["ovp_exact"]), "OSM critical infrastructure")
    if osm is not None and not osm.empty:
        pts_osm = osm[osm.geometry.geom_type == "Point"]
        poly_osm = osm[osm.geometry.geom_type == "Polygon"]
        for tag, pattern, color, label, marker, size in C.OSM_INFRA_STYLE:
            for gdf, is_pt in ((pts_osm, True), (poly_osm, False)):
                if tag not in gdf.columns:
                    continue
                sub = gdf[gdf[tag].astype(str).str.match(pattern, na=False) & gdf[tag].notna()]
                if sub.empty:
                    continue
                if is_pt:
                    sub.plot(ax=ax, color=color, marker=marker, markersize=size, alpha=0.9, zorder=8,
                             edgecolors="white", linewidths=0.5)
                else:
                    sub.plot(ax=ax, color=color, alpha=0.7, zorder=4)
                handles.append(mpatches.Patch(color=color, label=f"OSM {label} ({len(sub)})"))
                for g in sub.geometry:
                    shapes.append(((g.buffer(buf) if is_pt else g).__geo_interface__, 1.0))
    if not handles:
        _not_available(ax, "No critical infrastructure mapped in this bbox\n(USGS + OSM)", "#555555", 9)
    else:
        ax.legend(handles=handles, loc="lower left", fontsize=7, title="CISA Critical Infrastructure",
                  framealpha=0.92, title_fontsize=8, ncol=2)
    finish(ax, fig, ctx, "Critical Infrastructure", os.path.join(out_dir, C.LAYER_FILES["infrastructure"] + ".png"),
           opts.png, source="USGS National Map Structures + OpenStreetMap  |  PPD-21 / CISA sectors")
    save_npy(rasterize_shapes(shapes, ctx), os.path.join(out_dir, C.LAYER_FILES["infrastructure"] + ".npy"))


# =============================================================================================
# 4. Buildings and roads (OSM)
# =============================================================================================
def buildings(ctx, out_dir, src: Sources, opts: Options):
    log.info("[4/5] Buildings and roads (OSM)")
    roads = src.overpass(f"[out:json][timeout:60];way['highway']({ctx['ovp_exact']});out body geom;", "OSM roads")
    bldgs = src.overpass(f"[out:json][timeout:60];way['building']({ctx['ovp_exact']});out body geom;", "OSM buildings")
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.set_facecolor("#1a1a2e")
    fig.patch.set_facecolor("#1a1a2e")
    add_basemap(ax, opts.basemap, alpha=0.35)
    handles, road_shapes, bldg_shapes = [], [], []
    road_buf = C.ROAD_BUFFER_M * ctx["deg_per_m"]
    if roads is not None and not roads.empty:
        lines = roads[roads.geometry.geom_type == "LineString"]
        if not lines.empty and "highway" in lines.columns:
            for hw, (color, lw, risk) in C.ROAD_STYLE.items():
                sub = lines[lines["highway"] == hw]
                if sub.empty:
                    continue
                sub.plot(ax=ax, color=color, linewidth=lw, zorder=4)
                road_shapes += [(g.buffer(road_buf).__geo_interface__, risk) for g in sub.geometry]
            other = lines[~lines["highway"].isin(C.ROAD_STYLE)]
            if not other.empty:
                other.plot(ax=ax, color="#888888", linewidth=0.4, zorder=3)
                road_shapes += [(g.buffer(road_buf).__geo_interface__, C.OTHER_ROAD_SCORE) for g in other.geometry]
        handles.append(mpatches.Patch(color="#fcd6a4", label=f"Roads ({len(lines)})"))
    if bldgs is not None and not bldgs.empty:
        polys = bldgs[bldgs.geometry.geom_type == "Polygon"]
        if not polys.empty:
            polys.plot(ax=ax, color="#c8a96e", edgecolor="#a0845c", linewidth=0.4, alpha=0.85, zorder=5)
            handles.append(mpatches.Patch(color="#c8a96e", label=f"Buildings ({len(polys)})"))
            bldg_shapes = [(g.__geo_interface__, 1.0) for g in polys.geometry]
    if not handles:
        _not_available(ax, "No OSM road/building data in this bbox", "white", 11)
    else:
        ax.legend(handles=handles, loc="lower left", fontsize=8, framealpha=0.85, title="Feature", title_fontsize=9)
    finish(ax, fig, ctx, "Buildings & Roads  |  OpenStreetMap", os.path.join(out_dir, C.LAYER_FILES["buildings"] + ".png"),
           opts.png, dark=True, source="© OpenStreetMap contributors (ODbL)  |  Overpass API")
    arr = rasterize_shapes(road_shapes, ctx)
    if bldg_shapes:                                  # buildings (1.0) override roads
        arr = np.maximum(arr, rasterize_shapes(bldg_shapes, ctx))
    save_npy(arr, os.path.join(out_dir, C.LAYER_FILES["buildings"] + ".npy"))


# =============================================================================================
# 5. Coastal hazards (FEMA zones + OSM water/wetland + NOAA stations)
# =============================================================================================
def _noaa_stations(src: Sources, ctx):
    import geopandas as gpd
    from shapely.geometry import Point
    # No region filter: the bbox decides which stations are kept, so this works outside Florida too.
    r = src.client.get(C.NOAA_STATIONS, {"type": "tidepredictions", "units": "english"},
                       timeout=30, label="NOAA Tides & Currents")
    if r is None:
        return None
    pad = C.NOAA_STATION_SEARCH_PAD_DEG
    rows = []
    try:
        for s in r.json().get("stations", []):
            lat, lon = float(s.get("lat", 0)), float(s.get("lng", 0))
            if ctx["ymin"] - pad < lat < ctx["ymax"] + pad and ctx["xmin"] - pad < lon < ctx["xmax"] + pad:
                rows.append({"station_id": s.get("id", ""), "name": s.get("name", ""),
                             "type": s.get("type", "R"), "geometry": Point(lon, lat)})
    except (ValueError, TypeError) as exc:
        log.warning("NOAA parse error: %s", exc)
        return None
    return gpd.GeoDataFrame(rows, crs="EPSG:4326") if rows else None


def coastal(ctx, out_dir, src: Sources, opts: Options):
    log.info("[5/5] Coastal hazards")
    pad = C.COASTAL_SEARCH_PAD_DEG
    x0, x1, y0, y1 = ctx["xmin"] - pad, ctx["xmax"] + pad, ctx["ymin"] - pad, ctx["ymax"] + pad
    fig, ax = plt.subplots(figsize=(12, 10))
    add_basemap(ax, opts.basemap, alpha=0.45)
    handles, zone_shapes, water_shapes = [], [], []

    flood_gdf = src.arcgis(f"{C.FEMA_NFHL_MAPSERVER}/28/query", "FEMA NFHL flood zones (coastal margin)",
                           f"{x0},{y0},{x1},{y1}", ctx["bbox_geom"])
    if flood_gdf is not None and not flood_gdf.empty and "FLD_ZONE" in flood_gdf.columns:
        flood_gdf["_c"] = flood_gdf["FLD_ZONE"].apply(zone_color)
        flood_gdf.plot(ax=ax, color=flood_gdf["_c"], edgecolor="none", alpha=0.45, zorder=2)
        for z in sorted(flood_gdf["FLD_ZONE"].dropna().unique())[:5]:
            handles.append(mpatches.Patch(color=zone_color(z), label=f"FEMA Zone {z}", alpha=0.7))
        zone_shapes = [(g.__geo_interface__, zone_risk(z))
                       for g, z in zip(flood_gdf.geometry, flood_gdf["FLD_ZONE"]) if g is not None]

    wq = f"""
[out:json][timeout:40];
(
  way["natural"~"^(water|wetland|coastline|beach)$"]({y0},{x0},{y1},{x1});
  way["waterway"]({y0},{x0},{y1},{x1});
  way["landuse"~"^(beach|wetland)$"]({y0},{x0},{y1},{x1});
);
out body geom;
"""
    wgdf = src.overpass(wq, "OSM water/wetland/coastline")
    if wgdf is not None and not wgdf.empty:
        polys, lines = wgdf[wgdf.geometry.geom_type == "Polygon"], wgdf[wgdf.geometry.geom_type == "LineString"]
        if not polys.empty:
            polys.plot(ax=ax, color="#4575b4", alpha=0.35, edgecolor="#2166ac", linewidth=0.4, zorder=3)
            water_shapes = [(g.__geo_interface__, C.COASTAL_WATER_SCORE) for g in polys.geometry]
        if not lines.empty:
            lines.plot(ax=ax, color="#2166ac", linewidth=1.0, alpha=0.6, zorder=3)
        handles.append(mpatches.Patch(color="#4575b4", label="Water / Wetland (OSM)", alpha=0.6))

    stations = _noaa_stations(src, ctx)
    if stations is not None and not stations.empty:
        for stype, (label, color, marker, size) in C.STATION_STYLE.items():
            sub = stations[stations["type"] == stype]
            if sub.empty:
                continue
            sub.plot(ax=ax, color=color, marker=marker, markersize=size, alpha=0.95, zorder=10,
                     edgecolors="white", linewidths=0.7)
            handles.append(mpatches.Patch(color=color, label=f"{label} ({len(sub)})"))
        if opts.png:
            for _, row in stations.iterrows():
                ax.annotate(row["name"], (row.geometry.x, row.geometry.y), textcoords="offset points",
                            xytext=(4, 4), fontsize=5.5, color="#111111", zorder=15,
                            bbox=dict(boxstyle="round,pad=0.1", facecolor="white", alpha=0.55, linewidth=0))
    if handles:
        ax.legend(handles=handles, loc="lower left", fontsize=7, title="Coastal Hazard Layers",
                  framealpha=0.92, title_fontsize=8)
    finish(ax, fig, ctx, "Coastal Hazards", os.path.join(out_dir, C.LAYER_FILES["coastal"] + ".png"), opts.png,
           source="NOAA Tides & Currents  |  FEMA NFHL  |  © OpenStreetMap contributors (ODbL)")
    # raster: max of FEMA zone risk and open-water presence
    arr = np.maximum(rasterize_shapes(zone_shapes, ctx), rasterize_shapes(water_shapes, ctx))
    save_npy(arr, os.path.join(out_dir, C.LAYER_FILES["coastal"] + ".npy"))


LAYER_FUNCS = {"flood": flood, "population": population, "infrastructure": infrastructure,
               "buildings": buildings, "coastal": coastal}
