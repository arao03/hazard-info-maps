"""Constants, scoring tables and plot styles.

Everything a user might reasonably want to tune lives here. The risk scores in ``ZONE_RISK`` are
*heuristic* ordinal scores, not an official FEMA product.
"""

# --- grid / tile settings ---------------------------------------------------------------------
IMG_W, IMG_H = 800, 640          # size of ArcGIS image-tile requests (fallback FEMA tile only)
NPY_PAD = 0.001                  # degrees of padding added around the bbox for the output grid
NPY_TARGET_PIXELS = 1_000_000    # approx. W*H of each output raster; aspect ratio follows the bbox

# Point / line buffers are defined in metres so output does not depend on grid resolution.
# (The research scripts used 4 px and 3 px at ~3.4 m/px, i.e. about 14 m and 10 m.)
INFRA_POINT_BUFFER_M = 14.0
ROAD_BUFFER_M = 10.0

# --- OpenStreetMap Overpass endpoints (tried in order) ------------------------------------------
# Public instances have usage policies (rate limits, fair use). Run your own instance for heavy use.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# --- service endpoints -----------------------------------------------------------------------
FEMA_NFHL_MAPSERVER = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer"
CENSUS_GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
CENSUS_REPORTER = "https://api.censusreporter.org/1.0/data/show/latest"
TIGERWEB_TRACTS = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
                   "TIGERweb/Tracts_Blocks/MapServer/0/query")
USGS_STRUCTURES = "https://carto.nationalmap.gov/arcgis/rest/services/structures/MapServer"
NOAA_STATIONS = "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"

# --- FEMA flood zone -> risk score (0 = safe, 1 = most dangerous) -------------------------------
ZONE_RISK = {
    "VE": 1.00, "V": 0.95,
    "AE": 0.85, "AO": 0.75, "AH": 0.75, "A": 0.70,
    "AR": 0.60, "A99": 0.55,
    "X": 0.15,
    "D": 0.20,
    "OPEN WATER": 0.50,
}
ZONE_COLORS = {
    "VE": "#a50026", "V": "#d73027",
    "AE": "#f46d43", "AH": "#fdae61", "AO": "#fee090", "A": "#fc8d59",
    "X": "#91bfdb", "D": "#e0f3f8", "OPEN WATER": "#4575b4",
}

# Zone colours as RGB for the FEMA fallback image tile (no map labels)
FEMA_TILE_COLORS = [
    ("VE", [165, 0, 38]), ("V", [215, 48, 39]), ("AE", [244, 109, 67]), ("AH", [253, 174, 97]),
    ("AO", [254, 224, 144]), ("A", [252, 141, 89]), ("AR", [254, 196, 79]), ("A99", [254, 227, 145]),
    ("X", [145, 191, 219]), ("D", [224, 243, 248]), ("OPEN WATER", [69, 117, 180]),
]

# --- Critical infrastructure (CISA sectors) --------------------------------------------------
# USGS National Map "structures" layers: id -> (label, colour, marker, size)
USGS_INFRA_LAYERS = {
    14: ("Hospital", "#d73027", "*", 160),
    15: ("Ambulance", "#d73027", "+", 110),
    16: ("Fire/EMS", "#e08214", "^", 110),
    18: ("Police", "#4575b4", "s", 100),
    4: ("Town Hall", "#6baed6", "s", 80),
    23: ("School", "#74add1", "o", 70),
    21: ("College", "#74add1", "o", 80),
    19: ("Prison", "#969696", "D", 65),
}

# OSM tag styles: (tag, regex, colour, legend label, marker, size)
OSM_INFRA_STYLE = [
    ("amenity", r"^(hospital|clinic|doctors)$", "#d73027", "Hospital/Clinic", "*", 120),
    ("amenity", r"^(fire_station|rescue_station)$", "#e08214", "Fire/Rescue", "^", 100),
    ("amenity", r"^(police|coast_guard)$", "#4575b4", "Police/CG", "s", 90),
    ("amenity", r"^(ambulance_station)$", "#d73027", "Ambulance", "+", 90),
    ("amenity", r"^(school|university|college)$", "#74add1", "School/Univ", "o", 70),
    ("amenity", r"^(shelter|government|townhall)$", "#6baed6", "Gov/Shelter", "s", 70),
    ("amenity", r"^(pharmacy|nursing_home)$", "#a6d96a", "Healthcare", "d", 65),
    ("amenity", r"^(fuel)$", "#fdae61", "Fuel", "o", 55),
    ("emergency", r".*", "#d73027", "Emergency", "+", 80),
    ("power", r"^(plant|substation|generator)$", "#fdae61", "Energy", "h", 80),
    ("man_made", r"^(water_tower|pumping_station|wastewater_plant|water_works)$", "#6baed6",
     "Water Infra", "D", 80),
    ("aeroway", r"^(aerodrome|helipad)$", "#9ecae1", "Airport/Heli", "*", 90),
    ("telecom", r"^(exchange|data_center)$", "#bdbdbd", "Telecom", "s", 70),
    ("building", r"^(hospital|fire_station|public|government|school|university|college)$", "#c8a96e",
     "Critical Bldg", "s", 60),
]

# --- Roads: highway tag -> (colour, line width, raster score) -----------------------------------
ROAD_STYLE = {
    "motorway": ("#e892a2", 2.5, 0.90), "trunk": ("#f9b29c", 2.0, 0.85),
    "primary": ("#fcd6a4", 1.8, 0.80), "secondary": ("#f7fabf", 1.4, 0.75),
    "tertiary": ("#ffffff", 1.1, 0.65), "residential": ("#dddddd", 0.9, 0.55),
    "unclassified": ("#cccccc", 0.7, 0.50), "service": ("#bbbbbb", 0.5, 0.45),
    "track": ("#aaaaaa", 0.5, 0.40), "path": ("#999999", 0.4, 0.35),
    "footway": ("#999999", 0.4, 0.35), "cycleway": ("#abd9e9", 0.5, 0.40),
}
OTHER_ROAD_SCORE = 0.40

# --- NOAA station types -------------------------------------------------------------------------
STATION_STYLE = {
    "R": ("Water Level (Real-Time)", "#08306b", "o", 90),
    "S": ("Physical Oceanographic", "#2171b5", "s", 80),
    "M": ("Meteorological", "#6baed6", "^", 75),
    "C": ("Current", "#c6dbef", "D", 65),
    "U": ("Tsunami", "#d73027", "v", 80),
    "E": ("Harmonic Tide Gauge", "#4292c6", "P", 75),
}
NOAA_STATION_SEARCH_PAD_DEG = 1.0     # stations within this many degrees of the bbox are plotted
COASTAL_SEARCH_PAD_DEG = 0.35         # extra margin for FEMA/OSM water used by the coastal layer
COASTAL_WATER_SCORE = 0.90            # raster score for open water / wetland

LAYER_FILES = {
    "flood": "map1_flood_hazard_zones",
    "population": "map2_population_density",
    "infrastructure": "map3_critical_infrastructure",
    "buildings": "map4_buildings_roads",
    "coastal": "map5_noaa_coastal_hazards",
}
