# hazard-info-maps

Build five **pixel-aligned rasters** for a disaster-response area from public data. Each is `float32`,
north-up (row 0 = north, column 0 = west), on one shared grid.

| File | Layer | Source | Values |
|---|---|---|---|
| `map1_flood_hazard_zones.npy` | Flood hazard | FEMA NFHL | heuristic zone risk, 0–1 |
| `map2_population_density.npy` | Population | Census ACS (B01003) via CensusReporter + TIGERweb tracts | people / km² |
| `map3_critical_infrastructure.npy` | Critical infrastructure | USGS National Map + OSM | 0 / 1 |
| `map4_buildings_roads.npy` | Buildings and roads | OSM | roads 0.35–0.9, buildings 1 |
| `map5_noaa_coastal_hazards.npy` | Coastal hazard | FEMA NFHL + OSM water + NOAA | 0–1 |

Each run also writes `metadata.json` (extent, grid, county, `failed_requests`, timestamp) and PNG previews.
Flood, population and infrastructure use US services; elsewhere those layers are empty and the failure is
logged in `metadata.json`.

## Install and use

```bash
pip install -e .                       # Python 3.9+; needs rasterio / geopandas (GDAL wheels)
export HAZARDMAPS_CONTACT="you@example.org"   # identify yourself to the data services

hazardmaps --bbox -81.9116 26.4005 -81.8819 26.4313 --name fort_myers_beach --out-dir out
hazardmaps --tif scene.geo.tif --out-dir out      # or --tif-dir DIR
```

Options: `--layers`, `--pixel-size-m` (default ≈ 1 M pixels), `--no-png`, `--cache-dir` / `--no-cache`,
`--overpass-url`, `--basemap`, `--skip-existing`. See `hazardmaps --help`.

## Resolution

All layers are burned onto the same grid, so they align exactly. No resampling is applied, and the sources
differ in native detail: population is constant per census tract (blocky), flood zones are coarse
polygons, buildings, roads and infrastructure are footprint level. Point and road buffers are set in
metres (`config.py`), so they do not depend on `--pixel-size-m`. Value ranges differ (population is
people/km²), so normalise per layer before combining.

## Data terms and caveats

* **OpenStreetMap** (ODbL): credit "© OpenStreetMap contributors" on any published output. The default
  Overpass endpoints are shared public instances: the tool paces and caches requests, but use
  `--overpass-url` with your own instance for heavy use. `--basemap` fetches OSM raster tiles, whose policy
  forbids bulk downloads.
* **FEMA, Census, USGS, NOAA** are US Government data; FEMA's NFHL is for planning, not legal flood
  determinations. CensusReporter is a volunteer-run mirror of ACS data.
* Zone risk scores in `config.ZONE_RISK` are ordinal heuristics, not an official product.
* Services return current data, so re-runs can differ. Use the response cache to reproduce a run.
* Not for operational or life-safety decisions. You are responsible for each service's terms of use.

## Validation

On an existing research scene (Fort Myers Beach, same 1073 × 931 grid) the output matched the original
rasters: population 100% identical, flood 99.93%, buildings/roads 99.84%, infrastructure IoU 0.955. The
coastal layer was not compared (the OSM water query was failing during the test).

## Develop

`pip install -e ".[dev]" && pytest` (offline tests). Modules in `src/hazardmaps/`: `config` (constants),
`net` (HTTP and cache), `geo` (grid and rasterising), `layers` (the five layers), `cli`.
