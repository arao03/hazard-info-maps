"""Command-line interface: ``hazardmaps --bbox XMIN YMIN XMAX YMAX --name NAME --out-dir out``."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import platform
import sys

from . import __version__
from .config import LAYER_FILES, NPY_PAD, NPY_TARGET_PIXELS
from .geo import make_ctx, tif_wgs84_bbox
from .layers import LAYER_FUNCS, Options
from .net import HttpClient, Sources

log = logging.getLogger("hazardmaps")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hazardmaps",
        description="Build five aligned disaster-response information maps (flood, population, critical "
                    "infrastructure, buildings & roads, coastal) from public data for a bounding box.")
    src = p.add_argument_group("area of interest (choose one)")
    src.add_argument("--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                     help="WGS84 lon/lat bounding box (west south east north)")
    src.add_argument("--tif", help="GeoTIFF whose footprint defines the bounding box")
    src.add_argument("--tif-dir", help="process every .tif / .geo.tif in this directory")
    p.add_argument("--name", help="output sub-folder name (default: derived from the input)")
    p.add_argument("--out-dir", default="maps", help="output root directory (default: maps)")
    p.add_argument("--layers", nargs="+", choices=list(LAYER_FUNCS), default=list(LAYER_FUNCS),
                   help="subset of layers to build (default: all)")
    g = p.add_argument_group("grid")
    g.add_argument("--pixel-size-m", type=float, help="output resolution in metres per pixel")
    g.add_argument("--target-pixels", type=int, default=NPY_TARGET_PIXELS,
                   help="approximate pixel count per raster when --pixel-size-m is not given")
    g.add_argument("--pad-deg", type=float, default=NPY_PAD, help="degrees of padding around the bbox")
    o = p.add_argument_group("output / network")
    o.add_argument("--no-png", action="store_true", help="skip PNG previews (rasters only, faster)")
    o.add_argument("--basemap", action="store_true",
                   help="draw an OpenStreetMap raster basemap under PNGs (needs contextily; respect tile policy)")
    o.add_argument("--cache-dir", default=".hazardmaps_cache", help="HTTP response cache (default: .hazardmaps_cache)")
    o.add_argument("--no-cache", action="store_true", help="do not read or write the response cache")
    o.add_argument("--overpass-url", action="append", help="Overpass endpoint (repeatable; default: public instances)")
    o.add_argument("--skip-existing", action="store_true", help="skip areas whose rasters already exist")
    o.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _areas(args) -> list:
    """List of (name, bbox dict, source description)."""
    if args.bbox:
        xmin, ymin, xmax, ymax = args.bbox
        if not (xmin < xmax and ymin < ymax):
            raise SystemExit("--bbox must be XMIN YMIN XMAX YMAX with XMIN < XMAX and YMIN < YMAX")
        name = args.name or f"bbox_{ymin:.4f}_{xmin:.4f}_{ymax:.4f}_{xmax:.4f}"
        return [(name, dict(xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax), "bbox")]
    tifs = []
    if args.tif:
        tifs = [args.tif]
    elif args.tif_dir:
        tifs = sorted(os.path.join(args.tif_dir, f) for f in os.listdir(args.tif_dir)
                      if f.endswith(".tif") or f.endswith(".tiff"))
    if not tifs:
        raise SystemExit("provide --bbox, --tif or --tif-dir (with at least one GeoTIFF)")
    out = []
    for t in tifs:
        stem = os.path.basename(t).replace(".geo.tif", "").replace(".tiff", "").replace(".tif", "")
        out.append((args.name if (args.name and len(tifs) == 1) else stem, tif_wgs84_bbox(t), os.path.basename(t)))
    return out


def run_area(name, bbox, origin, args, client, sources) -> str:
    out_dir = os.path.join(args.out_dir, name)
    if args.skip_existing and all(os.path.exists(os.path.join(out_dir, LAYER_FILES[k] + ".npy")) for k in args.layers):
        log.info("skipping %s (rasters already exist)", name)
        return out_dir
    os.makedirs(out_dir, exist_ok=True)
    ctx = make_ctx(bbox, client=client, pad=args.pad_deg, target_pixels=args.target_pixels,
                   pixel_size_m=args.pixel_size_m)
    log.info("%s: %.5f,%.5f -> %.5f,%.5f  grid %d x %d (%.1f m/px)  county=%s", name, bbox["xmin"], bbox["ymin"],
             bbox["xmax"], bbox["ymax"], ctx["npy_w"], ctx["npy_h"], ctx["m_per_px"], ctx["county_name"])
    client.failures.clear()
    opts = Options(png=not args.no_png, basemap=args.basemap)
    done = []
    for key in args.layers:
        LAYER_FUNCS[key](ctx, out_dir, sources, opts)
        done.append(LAYER_FILES[key] + ".npy")
    meta = {
        "name": name, "source": origin,
        "bbox_wgs84": bbox,
        "npy_extent": {"xmin": ctx["npy_xmin"], "ymin": ctx["npy_ymin"], "xmax": ctx["npy_xmax"],
                       "ymax": ctx["npy_ymax"], "pad_deg": ctx["pad"]},
        "npy_shape": {"height": ctx["npy_h"], "width": ctx["npy_w"]},
        "approx_pixel_size_m": round(ctx["m_per_px"], 3),
        "crs": "EPSG:4326", "row_0": "north (ymax)", "col_0": "west (xmin)",
        "county": {"name": ctx["county_name"], "state_fips": ctx["state_fips"], "county_fips": ctx["county_fips"]},
        "layers": done,
        "failed_requests": sorted(set(client.failures)),
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "software": {"hazardmaps": __version__, "python": platform.python_version()},
        "note": "Source services return their current data at run time; re-running later may give different "
                "rasters. Use the response cache to reproduce a run.",
    }
    with open(os.path.join(out_dir, "metadata.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    if meta["failed_requests"]:
        log.warning("%s: some requests failed (layers may be partially empty): %s", name, meta["failed_requests"])
    return out_dir


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
                        format="%(levelname)s %(message)s")
    if args.verbose == 0:
        logging.getLogger("hazardmaps").setLevel(logging.INFO)
    client = HttpClient(cache_dir=None if args.no_cache else args.cache_dir)
    sources = Sources(client, overpass_urls=args.overpass_url)
    for name, bbox, origin in _areas(args):
        out = run_area(name, bbox, origin, args, client, sources)
        print(f"done: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
