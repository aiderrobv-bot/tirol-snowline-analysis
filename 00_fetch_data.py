"""
00_fetch_data.py — Fetch everything once, save to disk.

Every chapter after this loads from these local files instead of hitting
Overpass / tiris / Overture again. Same "fetch once, process locally"
lesson as the earlier optimization — now applied as the project's actual
structure, not just a speed trick.

Can be run standalone, or called from the notebook via `!python 00_fetch_data.py`
(see notebook Chapter 0a). Every fetch checks for its output file first and
skips if already present, so re-running is fast and safe.

Run once: python 00_fetch_data.py
(Re-run any time the underlying source data needs refreshing.)
"""

import os
# Runs as its own process (e.g. when called via `!python` from the notebook),
# so it needs this fix independently -- a leaked conda GDAL_DRIVER_PATH/
# GDAL_DATA causes OPENSSL_3.2.0 errors on every GeoPandas file write.
for _var in ["LD_LIBRARY_PATH", "GDAL_DRIVER_PATH", "GDAL_DATA", "PROJ_LIB", "GDAL_PLUGINS_PATH"]:
    os.environ.pop(_var, None)

import time
import requests
import duckdb
import urllib.request
import json
import geopandas as gpd
from shapely.geometry import LineString

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
WCS_URL = "https://gis.tirol.gv.at/arcgis/services/Service_Public/terrain/MapServer/WCSServer"
HEADERS = {"User-Agent": "tirol-snowline-project/0.4 (portfolio GIS project)"}
TIROL_BBOX = (10.0, 46.7, 12.7, 47.8)  # minx, miny, maxx, maxy

RESTAURANT_BAR_CATEGORIES = [
    "afghan_restaurant", "american_restaurant", "arabian_restaurant", "asian_fusion_restaurant",
    "asian_restaurant", "australian_restaurant", "austrian_restaurant", "bar", "bar_and_grill_restaurant",
    "barbecue_restaurant", "beer_bar", "breakfast_and_brunch_restaurant", "buffet_restaurant",
    "burger_restaurant", "caribbean_restaurant", "chicken_restaurant", "chinese_restaurant", "cocktail_bar",
    "comfort_food_restaurant", "cuban_restaurant", "dive_bar", "dumpling_restaurant",
    "eastern_european_restaurant", "european_restaurant", "fast_food_restaurant", "fish_and_chips_restaurant",
    "fondue_restaurant", "french_restaurant", "gay_bar", "georgian_restaurant", "german_restaurant",
    "gluten_free_restaurant", "greek_restaurant", "haute_cuisine_restaurant", "health_food_restaurant",
    "himalayan_nepalese_restaurant", "hookah_bar", "hot_dog_restaurant", "hotel_bar", "hungarian_restaurant",
    "indian_restaurant", "international_restaurant", "israeli_restaurant", "italian_restaurant",
    "japanese_restaurant", "korean_restaurant", "lebanese_restaurant", "mediterranean_restaurant",
    "mexican_restaurant", "middle_eastern_restaurant", "molecular_gastronomy_restaurant",
    "pan_asian_restaurant", "pizza_restaurant", "restaurant", "romanian_restaurant", "salad_bar",
    "seafood_restaurant", "smoothie_juice_bar", "soup_restaurant", "southern_restaurant",
    "spanish_restaurant", "sports_bar", "sushi_restaurant", "swiss_restaurant", "syrian_restaurant",
    "taco_restaurant", "tapas_bar", "texmex_restaurant", "thai_restaurant", "theme_restaurant",
    "turkish_restaurant", "vegan_restaurant", "vegetarian_restaurant", "vietnamese_restaurant", "wine_bar",
]


def run_overpass(query: str) -> dict:
    last_error = None
    for url in OVERPASS_URLS:
        for attempt in range(2):
            try:
                timeout = 90 if attempt == 0 else 150
                resp = requests.post(url, data={"data": query}, headers=HEADERS, timeout=timeout)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_error = e
                time.sleep(5)
    raise RuntimeError(f"Overpass failed: {last_error}")


def fetch_pistes():
    print("Fetching all Tirol pistes...")
    s, w, n, e = TIROL_BBOX[1], TIROL_BBOX[0], TIROL_BBOX[3], TIROL_BBOX[2]
    query = f"""
    [out:json][timeout:120];
    ( way["piste:type"="downhill"]({s},{w},{n},{e}); );
    out geom tags;
    """
    elements = run_overpass(query).get("elements", [])
    rows = []
    for el in elements:
        pts = el.get("geometry", [])
        if len(pts) < 2:
            continue
        tags = el.get("tags", {})
        rows.append({
            "difficulty": tags.get("piste:difficulty", "unknown"),
            "geometry": LineString([(p["lon"], p["lat"]) for p in pts]),
        })
    gdf = gpd.GeoDataFrame(rows, crs=4326)
    gdf.to_file("data_pistes.gpkg", driver="GPKG")
    print(f"  Saved {len(gdf)} piste segments to data_pistes.gpkg")


def fetch_venues():
    print("Fetching all Tirol venues (Overture Places via DuckDB)...")
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs;")
    con.sql("INSTALL spatial; LOAD spatial;")
    con.sql("SET s3_region='us-west-2';")
    with urllib.request.urlopen("https://stac.overturemaps.org/catalog.json", timeout=10) as r:
        release = json.loads(r.read())["latest"]
    print(f"  Overture release: {release}")

    places_path = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    minx, miny, maxx, maxy = TIROL_BBOX
    category_list = "', '".join(RESTAURANT_BAR_CATEGORIES)
    query = f"""
        SELECT names.primary AS name, categories.primary AS category,
               (bbox.xmin + bbox.xmax) / 2 AS lon,
               (bbox.ymin + bbox.ymax) / 2 AS lat
        FROM read_parquet('{places_path}')
        WHERE bbox.xmin BETWEEN {minx} AND {maxx}
          AND bbox.ymin BETWEEN {miny} AND {maxy}
          AND categories.primary IN ('{category_list}')
    """
    df = con.sql(query).df()
    gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs=4326)
    gdf.to_file("data_venues.gpkg", driver="GPKG")
    print(f"  Saved {len(gdf)} venues to data_venues.gpkg")


def fetch_dem():
    print("Fetching Tirol DEM...")
    minx, miny, maxx, maxy = TIROL_BBOX
    params = {
        "SERVICE": "WCS", "VERSION": "1.0.0", "REQUEST": "GetCoverage",
        "COVERAGE": "Gelaendemodell_5m_M28",
        "BBOX": f"{minx},{miny},{maxx},{maxy}",
        "CRS": "EPSG:4326", "RESPONSE_CRS": "EPSG:4326",
        "FORMAT": "GeoTIFF", "WIDTH": "2600", "HEIGHT": "1200",
    }
    last_error = None
    for attempt in range(4):
        try:
            resp = requests.get(WCS_URL, params=params, timeout=120)
            resp.raise_for_status()
            with open("data_dem.tif", "wb") as f:
                f.write(resp.content)
            print(f"  Saved {len(resp.content)/1e6:.1f} MB to data_dem.tif")
            return
        except Exception as e:
            last_error = e
            wait = 10 * (attempt + 1)
            print(f"  Attempt {attempt + 1} failed ({e}) -- retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"DEM fetch failed after 4 attempts: {last_error}")


def fetch_resort_boundaries():
    """Official 'URP Schigebietsgrenzen' (Land Tirol ski area boundaries) --
    downloaded automatically so the pipeline is fully self-contained."""
    print("Fetching official Tirol ski area boundaries...")
    item_id = "71e6c2220f144b79b6c79b4c9ce60653_0"
    url = f"https://opendata.arcgis.com/api/v3/datasets/{item_id}/downloads/data?format=geojson&spatialRefId=4326"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    if "json" not in resp.headers.get("Content-Type", ""):
        raise RuntimeError(
            "Expected GeoJSON but got something else -- the ArcGIS Hub item ID "
            "may have changed. Check https://data-tiris.opendata.arcgis.com "
            "and search for 'Schigebietsgrenzen' to find the current item ID."
        )
    with open("urp_schigebietsgrenzen.geojson", "wb") as f:
        f.write(resp.content)
    print(f"  Saved {len(resp.content)/1e6:.1f} MB to urp_schigebietsgrenzen.geojson")


def fetch_tirol_boundary():
    """Tirol province outline -- used as map background context so the 103
    ski area polygons don't float on blank white space with no sense of
    scale or position (including showing the Osttirol exclave)."""
    print("Fetching Tirol province boundary...")
    query = """
    [out:json][timeout:60];
    relation["name"="Tirol"]["admin_level"="4"];
    out geom;
    """
    resp = requests.post(OVERPASS_URLS[0], data={"data": query}, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    elements = resp.json().get("elements", [])

    from shapely.geometry import shape
    from shapely.ops import polygonize, unary_union

    segments = []
    for el in elements:
        for member in el.get("members", []):
            pts = member.get("geometry", [])
            if len(pts) < 2:
                continue
            segments.append(shape({
                "type": "LineString",
                "coordinates": [(p["lon"], p["lat"]) for p in pts],
            }))
    polygon = unary_union(list(polygonize(segments)))
    gdf = gpd.GeoDataFrame({"name": ["Tirol"]}, geometry=[polygon], crs=4326)
    gdf.to_file("data_tirol_boundary.gpkg", driver="GPKG")
    print(f"  Saved Tirol boundary to data_tirol_boundary.gpkg")


if __name__ == "__main__":
    t0 = time.time()
    fetch_resort_boundaries()
    fetch_tirol_boundary()
    fetch_pistes()
    fetch_venues()
    fetch_dem()
    print(f"\nAll data fetched in {time.time()-t0:.0f}s.")
    print("Ready for 01_load_and_rank_resorts.py")
