"""build_powerplants.py: server/power_plants.npz (+ power_plants.provenance.json), the thermal power plants of the world
with their mapped cooling towers and chimneys, for the "Power plants" feature (server/powerplants.py: the map layer,
the plant stream's plumes and the generic plant kit; the building pipeline's real towers and chimneys).

Ported from devo343's Earth Power Plants add-on (its source/build_plants.py, used with the author's permission,
2026-11-27): the same rules for a site's state (running / closed but standing / demolished) from the same sources, now
merged with OpenStreetMap's mapped plants (GPPD stopped updating in 2021) and OSM's mapped towers and chimneys.

Inputs (one folder, --inputs DIR; --fetch downloads the pinned ones; each file's SHA-256 is recorded and checked):
  power_plant.parquet          GMWID (Geomermaids, https://gmwid.geomermaids.com): OpenStreetMap's power=plant
                               worldwide with the operating US plants EIA-860M has and OSM lacks (origin 'eia').
                               ODbL 1.0, (c) OpenStreetMap contributors. Only its latest build is published: the
                               build's version and hash go into the provenance.
  global_power_plant_database_v_1_3.zip   WRI Global Power Plant Database v1.3.0 (CC BY 4.0)
  trackers.json                Global Energy Monitor's Global Coal Plant Tracker, the copy in GreenInfo-Network/
                               coal-tracker-client (Aug 2023; CC BY 4.0): each coal unit's status
  nuclear_power_plants.csv     GeoNuclearData (cristianst85, from IAEA PRIS, Mar 2024; ODbL 1.0)
  powerplants.csv              powerplantmatching (PyPSA; CC BY 4.0): European units' closing years
  osm/band_*.json              Overpass API exports of man_made=chimney, man_made=cooling_tower and tower:type=cooling
                               (out tags bb), one per 15-degree longitude band (ODbL, (c) OpenStreetMap contributors)
  tools/data/power_plants_manual.json   the add-on's hand-checked list (demolished sites, towers demolished, ...)

The merged file is a derived database of OpenStreetMap and GeoNuclearData: ODbL 1.0 (CREDITS.md, SOURCES.md).

Output arrays (power_plants.npz, no pickles):
  plants, sorted by (lat, lon, key):  p_lat, p_lon int32 (1e-6 degree), p_fuel uint8 (index in FUELS), p_state uint8
    (0 running, 1 closed and standing), p_flags uint8 (FLAG_*), p_mw float32 (NaN unknown), p_closed int16 (year or
    0), p_radius float32 (m: the site's reach), p_osm int64 (OSM id, 0 none; p_osm_type 0 node 1 way 2 relation),
    p_name_off int32 (n + 1 offsets into names, UTF-8 bytes), names uint8.
  structures, sorted by (lat, lon, osm type, osm id):  s_lat, s_lon int32, s_kind uint8 (KIND_*), s_height float32 (m,
    NaN unmapped), s_diam float32 (m, NaN unknown), s_osm int64, s_osm_type uint8, s_building uint8 (1: also mapped
    as a building), s_plant int32 (index into plants, -1 none).

usage:
  python3 tools/build_powerplants.py --inputs ~/bnge-tests/powerplants/inputs --osm ~/bnge-tests/powerplants/osm
  python3 tools/build_powerplants.py --inputs DIR --fetch          (downloads the pinned inputs first)
"""
import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:
    pass
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "server", "power_plants.npz")
MANUAL = os.path.join(ROOT, "tools", "data", "power_plants_manual.json")
SCHEMA = 1

FUELS = "CNGOBT"             # coal, nuclear, gas, oil, biomass / waste, geothermal (the add-on's codes)
FLAG_TOWERS_GONE = 1         # closed, its cooling towers demolished, the rest standing (manual list)
FLAG_OSM_AREA = 2            # OpenStreetMap maps the plant (its point and reach from the mapped area)
FLAG_STATUS_LIST = 4         # matched to the status sources (GPPD / GEM / GeoNuclearData / manual)
FLAG_EIA = 8                 # an EIA-860M plant OSM lacks (GMWID origin 'eia')
KIND_COOLING, KIND_CHIMNEY, KIND_COOLING_CELLS = 0, 1, 2

# Pinned inputs: (file, url, sha256 or None: recorded, not checked (GMWID publishes only its latest build))
PINNED = (
    ("power_plant.parquet", "https://parquetry.geomermaids.com/gmwid/latest/power_plant.parquet", None),
    ("global_power_plant_database_v_1_3.zip",
     "https://wri-dataportal-prod.s3.amazonaws.com/manual/global_power_plant_database_v_1_3.zip",
     "59f3e57312a85eec3c281608e5f3c8f993d8fdad5dde179430dca42f2ac6ce99"),
    ("trackers.json", "https://raw.githubusercontent.com/GreenInfo-Network/coal-tracker-client/"
                      "91023b1f4dfdbf9c264170f7e85932e1de062576/data/trackers.json",
     "57a3e39c795780728ccb5f8bcbe768f1bf1a8410f63c7c18d273f613c16b4981"),
    ("nuclear_power_plants.csv", "https://raw.githubusercontent.com/cristianst85/GeoNuclearData/"
                                 "1bc8b4ac106af236902385b87e46c540b4864815/data/csv/denormalized/nuclear_power_plants.csv",
     "b66f02af4e4e3c091f065f2c81658a8a3bc03736ea8278891101ceaff1edd95c"),
    ("powerplants.csv", "https://raw.githubusercontent.com/PyPSA/powerplantmatching/"
                        "a36e67595bbe117079cd94ff22a87bd37ec8c317/powerplants.csv",
     "ceccce02edd33813216a264fe6c8e3281ce2249435a259c1b1a1b729ce4f3d78"),
)

# --- the add-on's status rules (build_plants.py) ----------------------------------------------------------------------
KEEP = {"Coal": ("C", 50), "Nuclear": ("N", 0), "Gas": ("G", 100), "Oil": ("O", 100), "Biomass": ("B", 30),
        "Waste": ("B", 30), "Geothermal": ("T", 20), "Petcoke": ("C", 50), "Cogeneration": ("G", 100)}
MIN_MW = {"C": 50, "N": 0, "G": 100, "O": 100, "B": 30, "T": 20}    # a plant smaller than this is left out ...
MIN_AREA_M2 = 40000.0        # ... unless OSM maps it this big (no output mapped) or maps its towers
MERGE_M = 1500.0
MATCH_M = 3000.0
OSM_MATCH_M = 2500.0         # a status-list site this near an OSM plant (or inside its box) is that plant
PPM_FUEL = {"Hard Coal": "C", "Lignite": "C", "Nuclear": "N", "Natural Gas": "G", "Oil": "O", "Solid Biomass": "B",
            "Waste": "B", "Geothermal": "T"}
NOW_YEAR = 2026
CLOSED_BY = 2024
EU_CLOSE_FUELS = "CON"
REACTOR_STANDING_SINCE = 2005
# OSM plant:source -> fuel (the first of a ';' list that maps)
OSM_FUEL = (("nuclear", "N"), ("coal", "C"), ("lignite", "C"), ("petroleum", "C"), ("oil_shale", "C"),
            ("geothermal", "T"), ("gas", "G"), ("oil", "O"), ("diesel", "O"), ("gasoil", "O"), ("biomass", "B"),
            ("waste", "B"), ("biogas", "B"), ("biofuel", "B"), ("wood", "B"), ("peat", "B"))
SITE_MAX_M = 3000.0          # a site's reach at most this (a mapped area's half diagonal)
STRUCT_REACH_M = 400.0       # a structure this far outside a site's reach is not the site's
CHIMNEY_MIN_H = 25.0         # m: a chimney mapped lower than this (a house's, a bakery's) is left out ...
CHIMNEY_MIN_D = 3.0          # ... and one with no height unless its footprint is at least this wide or it is a site's
DEMOLISHED = re.compile(r"^(demolished|removed|razed|destroyed|was|abandoned|ruins?)(:|$)")


def dist_m(a, b):
    r = math.pi / 180
    dla, dlo = (b[0] - a[0]) * r, (b[1] - a[1]) * r
    h = math.sin(dla / 2) ** 2 + math.cos(a[0] * r) * math.cos(b[0] * r) * math.sin(dlo / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1.0, math.sqrt(h)))


class Grid:
    """Points in 0.5 degree cells for neighbour searches (deterministic: cells in insertion order)."""

    def __init__(self, items, key=lambda x: (x["lat"], x["lon"])):
        self.key, self.cells = key, {}
        for it in items:
            self.add(it)

    def add(self, it):
        la, lo = self.key(it)
        self.cells.setdefault((int(math.floor(la * 2)), int(math.floor(lo * 2))), []).append(it)

    def near(self, lat, lon, m):
        ci, cj = int(math.floor(lat * 2)), int(math.floor(lon * 2))
        out = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for it in self.cells.get((ci + di, cj + dj), ()):
                    d = dist_m((lat, lon), self.key(it))
                    if d <= m:
                        out.append((d, it))
        out.sort(key=lambda t: t[0])
        return out


def year_of(s):
    try:
        return int(float(s)) if s not in (None, "") else 0
    except ValueError:
        return int(str(s)[:4]) if str(s)[:4].isdigit() else 0


def read_gppd(zpath):
    with zipfile.ZipFile(zpath) as z:
        text = z.read("global_power_plant_database.csv").decode("utf-8")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        k = KEEP.get(r["primary_fuel"])
        if not k:
            continue
        try:
            mw, lat, lon = float(r["capacity_mw"] or 0), float(r["latitude"]), float(r["longitude"])
        except ValueError:
            continue
        if mw < k[1]:
            continue
        rows.append({"id": r["gppd_idnr"], "name": r["name"].strip(), "fuel": k[0], "mw": mw, "lat": lat, "lon": lon,
                     "src": "gppd"})
    rows.sort(key=lambda p: (-p["mw"], p["id"]))
    out, grid = [], Grid([])
    for p in rows:                                  # co-located units into sites
        hit = grid.near(p["lat"], p["lon"], MERGE_M)
        if hit:
            q = hit[0][1]
            q["mw"] += p["mw"]
            if p["fuel"] == "N":
                q["fuel"] = "N"
            continue
        grid.add(p)
        out.append(p)
    return out


def coal_sites(gem):
    units = [u for u in gem if u.get("lat") is not None and u.get("lng") is not None
             and u.get("status") in ("operating", "retired", "mothballed")]
    sites, g = [], Grid([])
    for u in units:
        lat, lon = float(u["lat"]), float(u["lng"])
        near = g.near(lat, lon, MERGE_M)
        hit = near[0][1] if near else None
        if not hit:
            hit = {"lat": lat, "lon": lon, "name": u.get("plant", ""), "st": [], "mw": 0.0}
            sites.append(hit)
            g.add(hit)
        hit["st"].append(u["status"])
        try:
            hit["mw"] += float(u.get("capacity") or 0)
        except ValueError:
            pass
    return sites


def reactor_sites(rows):
    sites, g = [], Grid([])
    for r in rows:
        try:
            lat, lon = float(r["Latitude"]), float(r["Longitude"])
        except (TypeError, ValueError):
            continue
        st = r["Status"]
        if st not in ("Operational", "Shutdown", "Suspended Operation", "Decommissioning Completed", "Never Commissioned"):
            continue
        near = g.near(lat, lon, MATCH_M)
        hit = near[0][1] if near else None
        if not hit:
            hit = {"lat": lat, "lon": lon, "name": r["Name"].rsplit("-", 1)[0].strip(), "st": [], "to": 0, "mw": 0.0}
            sites.append(hit)
            g.add(hit)
        hit["st"].append(st)
        hit["to"] = max(hit["to"], year_of(r.get("OperationalTo")))
        try:
            hit["mw"] += float(r.get("Capacity") or 0)
        except ValueError:
            pass
    return sites


def ppm_units(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            fu = PPM_FUEL.get(r["Fueltype"])
            if not fu:
                continue
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
            except ValueError:
                continue
            out.append({"lat": lat, "lon": lon, "fuel": fu, "out": year_of(r["DateOut"]),
                        "mw": float(r["Capacity"] or 0)})
    return out


def status_list(inputs, manual_path):
    """The add-on's list: each thermal site's fuel, size and state ('A' running, 'I' closed and standing; the
    demolished ones dropped), from GPPD, GEM, GeoNuclearData, powerplantmatching and the manual list."""
    plants = read_gppd(os.path.join(inputs, "global_power_plant_database_v_1_3.zip"))
    with open(os.path.join(inputs, "trackers.json"), encoding="utf-8") as f:
        gem = json.load(f)
    with open(os.path.join(inputs, "nuclear_power_plants.csv"), encoding="utf-8") as f:
        nuc = list(csv.DictReader(f))
    with open(manual_path, encoding="utf-8") as f:
        manual = json.load(f)
    csites, nsites = coal_sites(gem), reactor_sites(nuc)
    ppm = ppm_units(os.path.join(inputs, "powerplants.csv"))
    cgrid, ngrid, pgrid = Grid(csites), Grid(nsites), Grid(ppm)
    used_c, used_n = set(), set()
    out = []
    for p in plants:
        p["state"], p["closed"], p["flags"] = "A", 0, ""
        if p["fuel"] == "C":
            near = cgrid.near(p["lat"], p["lon"], MATCH_M)
            if near:
                sts = []
                for d, s in near:
                    sts += s["st"]
                    used_c.add(id(s))
                if "operating" not in sts:
                    conv = [u for d, u in pgrid.near(p["lat"], p["lon"], 2000)
                            if u["fuel"] in "BGO" and (u["out"] == 0 or u["out"] > NOW_YEAR) and u["mw"] >= 50]
                    if not conv:
                        p["state"] = "I"
        elif p["fuel"] == "N":
            near = ngrid.near(p["lat"], p["lon"], MATCH_M + 1000)
            if near:
                sts, to = [], 0
                for d, s in near:
                    sts += s["st"]
                    to = max(to, s["to"])
                    used_n.add(id(s))
                if "Operational" not in sts:
                    if all(x == "Decommissioning Completed" for x in sts) or (to and to < REACTOR_STANDING_SINCE):
                        p["state"] = "X"
                    else:
                        p["state"], p["closed"] = "I", to
        if p["state"] == "A" and p["fuel"] in EU_CLOSE_FUELS:
            near = [u for d, u in pgrid.near(p["lat"], p["lon"], 2000) if u["fuel"] == p["fuel"]
                    or (p["fuel"] in "CO" and u["fuel"] in "CO")]
            if near and all(0 < u["out"] <= CLOSED_BY for u in near):
                p["state"], p["closed"] = "I", max(u["out"] for u in near)
        out.append(p)
    for k, s in enumerate(csites):
        if id(s) in used_c or s["mw"] < 100:
            continue
        if "operating" not in s["st"] and "mothballed" in s["st"]:
            out.append({"id": f"GEM-{k}", "name": s["name"], "fuel": "C", "mw": s["mw"], "lat": s["lat"],
                        "lon": s["lon"], "state": "I", "closed": 0, "flags": "", "src": "gem"})
    for k, s in enumerate(nsites):
        if id(s) in used_n or "Operational" in s["st"] or "Never Commissioned" in s["st"]:
            continue
        if any(x in ("Shutdown", "Suspended Operation") for x in s["st"]) and s["to"] >= REACTOR_STANDING_SINCE:
            out.append({"id": f"NUC-{k}", "name": s["name"] + " Nuclear Power Plant", "fuel": "N",
                        "mw": s["mw"] or 900, "lat": s["lat"], "lon": s["lon"], "state": "I", "closed": s["to"],
                        "flags": "", "src": "nuc"})
    for e in manual.get("entries", []):
        hits = [p for p in out if (not e.get("fuel") or e["fuel"] == p["fuel"])
                and dist_m((p["lat"], p["lon"]), (e["lat"], e["lon"])) <= e.get("radius_m", 4000)]
        if not hits and e["state"] in ("standing", "closed"):
            out.append({"id": "MAN-" + e["name"].replace(" ", "_"), "name": e["name"], "fuel": e["fuel"],
                        "mw": e.get("mw", 1000), "lat": e["lat"], "lon": e["lon"], "state": "I",
                        "closed": e.get("closed", 0), "flags": e.get("flags", ""), "src": "manual"})
            continue
        for p in hits:
            if e["state"] == "demolished":
                p["state"] = "X"
            elif e["state"] == "towers_gone":
                p["state"], p["flags"], p["closed"] = "I", "t", e.get("closed", p["closed"])
            elif e["state"] in ("standing", "closed"):
                p["state"], p["closed"] = "I", e.get("closed", p["closed"])
            elif e["state"] == "running":
                p["state"], p["closed"], p["flags"] = "A", 0, ""
    demolished = [p for p in out if p["state"] == "X"]
    return [p for p in out if p["state"] != "X"], demolished, manual


# --- OpenStreetMap ------------------------------------------------------------------------------------------------------
def osm_fuel(source):
    s = str(source or "").lower()
    for part in re.split(r"[;,]", s):
        part = part.strip()
        for word, code in OSM_FUEL:
            if word in part:
                return code
    return None


def read_osm_plants(path):
    """GMWID's thermal plants: [{lat, lon, fuel, mw, radius, name, osm, osm_type, closed, eia, start}]."""
    import duckdb
    con = duckdb.connect()
    rows = con.execute(
        "SELECT osm_id, osm_type, origin, lifecycle, name, source, output_mw, output_mw_estimated, area_m2, "
        "bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax, start_date FROM read_parquet(?) "
        "WHERE lifecycle IN ('active', 'disused') AND origin IN ('osm', 'eia') ORDER BY osm_type, osm_id, origin",
        [path]).fetchall()
    out = []
    for oid, otype, origin, life, name, source, mw, mwe, area, x0, y0, x1, y1, start in rows:
        fuel = osm_fuel(source)
        if fuel is None or x0 is None:
            continue
        lat, lon = (float(y0) + float(y1)) / 2, (float(x0) + float(x1)) / 2
        half = dist_m((float(y0), float(x0)), (float(y1), float(x1))) / 2
        mwv = mw if mw is not None else mwe
        out.append({"lat": lat, "lon": lon, "fuel": fuel, "mw": float(mwv) if mwv is not None else float("nan"),
                    "area": float(area) if area is not None else 0.0, "radius": min(max(half, 60.0), SITE_MAX_M),
                    "box": (float(y0), float(x0), float(y1), float(x1)), "name": (name or "").strip(),
                    "osm": int(oid or 0) if origin == "osm" else 0,
                    "osm_type": {"node": 0, "way": 1, "relation": 2}.get(otype, 0), "closed": life == "disused",
                    "eia": origin == "eia"})
    return out


def length_m(v):
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*(m|metres?|meters?|ft|feet|')?\s*$", str(v or ""), re.I)
    if not m:
        return float("nan")
    x = float(m.group(1)) * (0.3048 if (m.group(2) or "").lower() in ("ft", "feet", "'") else 1.0)
    return x if 0.5 <= x <= 600 else float("nan")


def read_structures(osm_dir):
    """The mapped cooling towers and chimneys: {(type, id): {lat, lon, kind, height, diam, building}}."""
    out = {}
    for name in sorted(os.listdir(osm_dir)):
        if not (name.startswith("band_") and name.endswith(".json")):
            continue
        with open(os.path.join(osm_dir, name), encoding="utf-8") as f:
            d = json.load(f)
        for e in d.get("elements", ()):
            tags = e.get("tags") or {}
            if any(DEMOLISHED.match(k) for k in tags) or tags.get("disused:man_made") or tags.get("end_date"):
                continue
            mm = tags.get("man_made")
            if mm == "chimney":
                kind = KIND_CHIMNEY
            elif mm == "cooling_tower" or tags.get("tower:type") == "cooling":
                kind = KIND_COOLING
            else:
                continue
            t = {"node": 0, "way": 1, "relation": 2}.get(e.get("type"))
            if t is None:
                continue
            b = e.get("bounds")
            if t == 0:
                if "lat" not in e:
                    continue
                lat, lon, diam = float(e["lat"]), float(e["lon"]), float("nan")
            elif b:
                lat, lon = (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
                w = dist_m((lat, b["minlon"]), (lat, b["maxlon"]))
                h = dist_m((b["minlat"], lon), (b["maxlat"], lon))
                diam = (w + h) / 2
                if kind == KIND_COOLING and (min(w, h) < 0.75 * max(w, h) or diam < 25.0):
                    kind = KIND_COOLING_CELLS    # (a mechanical-draft cell bank: a long block, or small)
                if diam > (250.0 if kind != KIND_CHIMNEY else 60.0):
                    continue                     # (an area round them, not one structure)
            else:
                continue
            dtag = length_m(tags.get("diameter"))
            if math.isfinite(dtag):
                diam = dtag
            if kind == KIND_COOLING and t == 0:
                method = (tags.get("cooling:method") or tags.get("tower:construction") or "").lower()
                if "mechanical" in method or "forced" in method or "fan" in method:
                    kind = KIND_COOLING_CELLS
            out[(t, int(e["id"]))] = {"lat": lat, "lon": lon, "kind": kind, "height": length_m(tags.get("height")),
                                      "diam": diam, "building": 1 if tags.get("building") not in (None, "no") else 0}
    return out


# --- merging ------------------------------------------------------------------------------------------------------------
def merge(osm, listed):
    """OSM plants first (their place and reach), each taking the status list's state for its site; the list's sites
    OSM lacks kept at their point."""
    lgrid = Grid(listed)
    used = set()
    plants = []
    for o in sorted(osm, key=lambda o: (o["osm_type"], o["osm"], o["lat"], o["lon"])):
        p = dict(o, state=1 if o["closed"] else 0, closed_year=0, towers_gone=False, listed=False)
        y0, x0, y1, x1 = o["box"]
        best = None
        for d, s in lgrid.near(o["lat"], o["lon"], max(OSM_MATCH_M, o["radius"] + 500)):
            if id(s) in used:
                continue
            inside = y0 - 0.005 <= s["lat"] <= y1 + 0.005 and x0 - 0.005 <= s["lon"] <= x1 + 0.005
            same = s["fuel"] == o["fuel"] or (s["fuel"] in "CO" and o["fuel"] in "CO") or s["fuel"] == "N" or \
                o["fuel"] == "N"
            # (inside the mapped area: that plant whatever its fuel was, a coal site converted to biomass or gas is
            # still the site: Drax, coal in GPPD, biomass in OSM; nearby only: of the same fuel)
            if inside or (d <= OSM_MATCH_M and same):
                best = s
                break
        if best is not None:
            used.add(id(best))
            p["listed"] = True
            same = best["fuel"] == o["fuel"] or (best["fuel"] in "CO" and o["fuel"] in "CO")
            if best["state"] == "I" and same:       # (a site's old fuel closed, OSM mapping it on another: running)
                p["state"], p["closed_year"] = 1, int(best.get("closed") or 0)
            p["towers_gone"] = "t" in (best.get("flags") or "")
            if best["fuel"] == "N":
                p["fuel"] = "N"
            if not math.isfinite(p["mw"]):
                p["mw"] = float(best["mw"])
            if not p["name"]:
                p["name"] = best["name"]
        plants.append(p)
    ogrid = Grid([p for p in plants if p["osm"] or p["eia"]])
    for s in listed:
        if id(s) in used:
            continue
        # (another unit list of a site OSM maps: GPPD's Bruce A and Bruce B are one mapped Bruce station; never a
        # second, generic plant beside the real one)
        if any(d <= max(2000.0, o["radius"] + 1000.0) for d, o in ogrid.near(s["lat"], s["lon"], SITE_MAX_M + 1000.0)):
            continue
        # (... or one OSM maps by the same name farther off: GPPD's "Kozienice" point lies 12 km from the mapped
        # Elektrownia Kozienice, and a generic plant stood in a village there, in game 2026-11-27)
        words = name_words(s["name"])
        if words and any(words & name_words(o["name"]) for _d, o in ogrid.near(s["lat"], s["lon"], NAME_MATCH_M)):
            continue
        plants.append({"lat": s["lat"], "lon": s["lon"], "fuel": s["fuel"], "mw": float(s["mw"]), "area": 0.0,
                       "radius": 0.0, "name": s["name"], "osm": 0, "osm_type": 0, "eia": False, "listed": True,
                       "state": 1 if s["state"] == "I" else 0, "closed_year": int(s.get("closed") or 0),
                       "towers_gone": "t" in (s.get("flags") or "")})
    return plants


NAME_MATCH_M = 30000.0       # a status-list site this near an OSM plant of the same name is that plant
GENERIC_WORDS = {"power", "station", "plant", "generating", "energy", "thermal", "nuclear", "electric", "electricity",
                 "centrale", "central", "kraftwerk", "heizkraftwerk", "elektrownia", "elektrocieplownia", "unit",
                 "units", "complex", "project", "company", "limited", "facility", "combined", "cycle", "steam",
                 "electrica", "termica", "termoelectrica", "nucleaire", "thermique", "atomic", "fossil", "plants"}


def name_words(name):
    """A plant name's distinctive words (lower case, five letters or more, not GENERIC_WORDS)."""
    return {w for w in re.findall(r"[^\W\d_]{5,}", str(name or "").lower()) if w not in GENERIC_WORDS}


def demolished_site(p, demolished):
    return any(dist_m((p["lat"], p["lon"]), (d["lat"], d["lon"])) <= 3000 and
               (d["fuel"] == p["fuel"] or d["fuel"] == "N" or p["fuel"] == "N") for d in demolished)


def assign(plants, structs):
    """Each structure's plant (the nearest whose reach holds it), -1 none; drops small unattached chimneys."""
    g = Grid([dict(i=i, lat=p["lat"], lon=p["lon"]) for i, p in enumerate(plants)])
    out = []
    for key in sorted(structs):
        s = structs[key]
        best, bd = -1, None
        for d, it in g.near(s["lat"], s["lon"], SITE_MAX_M + STRUCT_REACH_M):
            p = plants[it["i"]]
            reach = (p["radius"] if p["radius"] > 0 else 600.0) + STRUCT_REACH_M
            if d <= reach and (bd is None or d < bd):
                best, bd = it["i"], d
        if s["kind"] == KIND_CHIMNEY and best < 0:
            h, dm = s["height"], s["diam"]
            if not ((math.isfinite(h) and h >= CHIMNEY_MIN_H) or (not math.isfinite(h) and math.isfinite(dm)
                                                                  and dm >= CHIMNEY_MIN_D)):
                continue
        if s["kind"] == KIND_CHIMNEY and math.isfinite(s["height"]) and s["height"] < CHIMNEY_MIN_H and best < 0:
            continue
        out.append(dict(s, osm_type=key[0], osm=key[1], plant=best))
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch(inputs):
    os.makedirs(inputs, exist_ok=True)
    for name, url, want in PINNED:
        dst = os.path.join(inputs, name)
        if not os.path.exists(dst):
            req = urllib.request.Request(url, headers={"User-Agent": "BeamNG.Earth-data-build"})
            with urllib.request.urlopen(req, timeout=300) as r, open(dst + ".part", "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            os.replace(dst + ".part", dst)
        if want and sha256(dst) != want:
            sys.exit(f"{name}: SHA-256 differs from the pinned {want}")


def build(inputs, osm_dir, out=OUT):
    for name, _url, want in PINNED:
        p = os.path.join(inputs, name)
        if not os.path.exists(p):
            sys.exit(f"missing input {p} (--fetch downloads it)")
        if want and sha256(p) != want:
            sys.exit(f"{name}: SHA-256 differs from the pinned {want}")
    listed, demolished, _manual = status_list(inputs, MANUAL)
    osm = read_osm_plants(os.path.join(inputs, "power_plant.parquet"))
    plants = merge(osm, listed)
    structs = read_structures(osm_dir)
    # (a site the manual list or the reactor list has demolished is left out even where OSM still maps it)
    plants = [p for p in plants if not (not p["listed"] and demolished_site(p, demolished))]
    pre = assign(plants, structs)
    with_struct = {s["plant"] for s in pre if s["plant"] >= 0}
    keep = []
    for i, p in enumerate(plants):
        big = math.isfinite(p["mw"]) and p["mw"] >= MIN_MW[p["fuel"]]
        if big or p["fuel"] == "N" or i in with_struct or (not math.isfinite(p["mw"]) and p["area"] >= MIN_AREA_M2):
            keep.append(p)
    keep.sort(key=lambda p: (round(p["lat"] * 1e6), round(p["lon"] * 1e6), p["osm_type"], p["osm"], p["name"]))
    structs = assign(keep, structs)
    structs.sort(key=lambda s: (round(s["lat"] * 1e6), round(s["lon"] * 1e6), s["osm_type"], s["osm"]))
    names = [p["name"].encode("utf-8")[:120] for p in keep]
    off = np.zeros(len(keep) + 1, np.int32)
    off[1:] = np.cumsum([len(b) for b in names])
    flags = np.array([(FLAG_TOWERS_GONE if p["towers_gone"] else 0) | (FLAG_OSM_AREA if p["osm"] else 0)
                      | (FLAG_STATUS_LIST if p["listed"] else 0) | (FLAG_EIA if p["eia"] else 0) for p in keep], np.uint8)
    arrays = dict(
        schema=np.array([SCHEMA], np.int32),
        p_lat=np.array([round(p["lat"] * 1e6) for p in keep], np.int32),
        p_lon=np.array([round(p["lon"] * 1e6) for p in keep], np.int32),
        p_fuel=np.array([FUELS.index(p["fuel"]) for p in keep], np.uint8),
        p_state=np.array([p["state"] for p in keep], np.uint8),
        p_flags=flags,
        p_mw=np.array([p["mw"] for p in keep], np.float32),
        p_closed=np.array([p["closed_year"] for p in keep], np.int16),
        p_radius=np.array([p["radius"] for p in keep], np.float32),
        p_osm=np.array([p["osm"] for p in keep], np.int64),
        p_osm_type=np.array([p["osm_type"] for p in keep], np.uint8),
        p_name_off=off,
        names=np.frombuffer(b"".join(names), np.uint8).copy(),
        s_lat=np.array([round(s["lat"] * 1e6) for s in structs], np.int32),
        s_lon=np.array([round(s["lon"] * 1e6) for s in structs], np.int32),
        s_kind=np.array([s["kind"] for s in structs], np.uint8),
        s_height=np.array([s["height"] for s in structs], np.float32),
        s_diam=np.array([s["diam"] for s in structs], np.float32),
        s_osm=np.array([s["osm"] for s in structs], np.int64),
        s_osm_type=np.array([s["osm_type"] for s in structs], np.uint8),
        s_building=np.array([s["building"] for s in structs], np.uint8),
        s_plant=np.array([s["plant"] for s in structs], np.int32),
    )
    tmp = out + ".tmp.npz"
    np.savez_compressed(tmp, **arrays)
    os.replace(tmp, out)
    by = {}
    for p in keep:
        k = p["fuel"] + ("I" if p["state"] else "A")
        by[k] = by.get(k, 0) + 1
    kinds = {k: int((arrays["s_kind"] == k).sum()) for k in (KIND_COOLING, KIND_CHIMNEY, KIND_COOLING_CELLS)}
    osm_files = sorted(n for n in os.listdir(osm_dir) if n.startswith("band_") and n.endswith(".json"))
    stamps = set()
    for n in osm_files:
        with open(os.path.join(osm_dir, n), encoding="utf-8") as f:
            stamps.add(json.load(f).get("osm3s", {}).get("timestamp_osm_base", ""))
    prov = {
        "schema": SCHEMA,
        "description": "Thermal power plants worldwide with their mapped cooling towers and chimneys",
        "built_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs": {name: {"url": url, "sha256": sha256(os.path.join(inputs, name)),
                          "bytes": os.path.getsize(os.path.join(inputs, name))} for name, url, _w in PINNED},
        "osm_structures": {"query": "nwr[man_made=chimney]; nwr[man_made=cooling_tower]; nwr[tower:type=cooling]; "
                                    "out tags bb (Overpass API, 15-degree longitude bands)",
                           "osm_base": sorted(stamps), "files": {n: sha256(os.path.join(osm_dir, n)) for n in osm_files}},
        "manual": {"file": "tools/data/power_plants_manual.json", "sha256": sha256(MANUAL)},
        "license": "ODbL-1.0 (a derived database of OpenStreetMap and GeoNuclearData); GPPD, GEM and "
                   "powerplantmatching data CC BY 4.0",
        "attribution": "(c) OpenStreetMap contributors; World Resources Institute et al. (Global Power Plant Database "
                       "v1.3.0); Global Energy Monitor (Global Coal Plant Tracker); GeoNuclearData (Cristian Stoica, "
                       "IAEA PRIS); powerplantmatching (PyPSA); U.S. EIA (EIA-860M via GMWID); plant rules from "
                       "devo343's Earth Power Plants",
        "fuels": dict(zip(FUELS, ("coal", "nuclear", "gas", "oil", "biomass", "geothermal"))),
        "counts": {"plants": len(keep), "by_fuel_state": dict(sorted(by.items())), "structures": kinds,
                   "structures_in_plants": int((arrays["s_plant"] >= 0).sum()),
                   "osm_plants": int(((flags & FLAG_OSM_AREA) > 0).sum()), "eia_plants": int(((flags & FLAG_EIA) > 0).sum()),
                   "status_list_only": int(((flags & (FLAG_OSM_AREA | FLAG_EIA)) == 0).sum())},
        "npz_sha256": sha256(out),
        "npz_bytes": os.path.getsize(out),
    }
    with open(os.path.splitext(out)[0] + ".provenance.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(prov, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(json.dumps(prov["counts"]), prov["npz_bytes"], "bytes", prov["npz_sha256"])
    return prov


# --- the weekly data-repo build (BeamNG.Earth-data .github/workflows/powerplants.yml) ----------------------------------
OVERPASS = ("https://overpass-api.de/api/interpreter", "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter")
OVERPASS_HOSTS = {"overpass-api.de", "maps.mail.ru", "overpass.kumi.systems"}
BAND_DEG = 15
MAX_BAND_BYTES = 256 << 20
# Plants any build must hold (validate): (name word, lat, lon, fuel letter, mapped cooling towers at least)
ANCHORS = (("drax", 53.737, -0.997, "B", 12), ("gavin", 38.935, -82.116, "C", 2), ("cattenom", 49.416, 6.218, "N", 4),
           ("niederau", 50.996, 6.670, "C", 6), ("irsching", 48.766, 11.584, "G", 0))
GROWTH = (0.9, 1.15)        # a build's plants and structures against the published one's: at most this much change


def fetch_osm(out_dir, tries=9, wait_s=90):
    """The Overpass export of the mapped chimneys and cooling towers, one file per BAND_DEG of longitude (out tags bb),
    each checked as JSON with no runtime error remark; mirrors in turn, waits between tries. Exits on a band that never
    came."""
    import time
    from urllib.parse import urlencode, urlparse
    os.makedirs(out_dir, exist_ok=True)
    for lo in range(-180, 180, BAND_DEG):
        hi = lo + BAND_DEG
        out = os.path.join(out_dir, f"band_{lo}.json")
        q = (f'[out:json][timeout:400][maxsize:268435456];(nwr["man_made"="chimney"](-90,{lo},90,{hi});'
             f'nwr["man_made"="cooling_tower"](-90,{lo},90,{hi});nwr["tower:type"="cooling"](-90,{lo},90,{hi}););'
             f'out tags bb;')
        for k in range(tries):
            url = OVERPASS[k % len(OVERPASS)]
            assert urlparse(url).hostname in OVERPASS_HOSTS
            try:
                req = urllib.request.Request(url, data=urlencode({"data": q}).encode(),
                                             headers={"User-Agent": "BeamNG.Earth-data-build"})
                with urllib.request.urlopen(req, timeout=900) as r:
                    body = r.read(MAX_BAND_BYTES + 1)
                if len(body) > MAX_BAND_BYTES:
                    raise ValueError("band too large")
                d = json.loads(body)
                remark = str(d.get("remark") or "")
                if "error" in remark.lower() or "runtime" in remark.lower():
                    raise ValueError(remark[:200])
                if not isinstance(d.get("elements"), list):
                    raise ValueError("no elements")
                with open(out + ".part", "wb") as f:
                    f.write(body)
                os.replace(out + ".part", out)
                print(f"band {lo}: {len(d['elements'])} elements", flush=True)
                break
            except Exception as e:      # (a busy server: the next mirror, later)
                print(f"band {lo} try {k + 1}: {type(e).__name__}: {str(e)[:160]}", flush=True)
                time.sleep(wait_s)
        else:
            sys.exit(f"band {lo}: no answer after {tries} tries")


def stage(inputs, osm_dir, out_dir, version):
    """The published form in out_dir: power_plants.npz, provenance.json and manifest.json (format 1, name, version,
    build: 16 hex of the npz's SHA-256, plants, structures, files [{path, size, sha256}])."""
    os.makedirs(out_dir, exist_ok=True)
    npz = os.path.join(out_dir, "power_plants.npz")
    prov = build(inputs, osm_dir, npz)
    os.replace(os.path.join(out_dir, "power_plants.provenance.json"), os.path.join(out_dir, "provenance.json"))
    digest = sha256(npz)
    m = {"format": 1, "name": "powerplants", "version": version, "build": digest[:16],
         "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "attribution": "(c) OpenStreetMap contributors (ODbL 1.0); World Resources Institute et al., Global Power "
                        "Plant Database (CC BY 4.0); Global Energy Monitor (CC BY 4.0); GeoNuclearData (ODbL); "
                        "powerplantmatching (CC BY 4.0)",
         "plants": prov["counts"]["plants"], "structures": sum(prov["counts"]["structures"].values()),
         "files": [{"path": "power_plants.npz", "size": os.path.getsize(npz), "sha256": digest}]}
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, indent=1, sort_keys=True)
        f.write("\n")
    return m


def validate(out_dir, previous=None):
    """A staged build checked before it may be published: the npz's columns (the server's own reader rules), the
    anchors (ANCHORS: known plants with their mapped towers), and its counts against the published build's manifest
    (previous: within GROWTH). Exits non-zero with the reasons."""
    bad = []
    with open(os.path.join(out_dir, "manifest.json"), encoding="utf-8") as f:
        m = json.load(f)
    npz = os.path.join(out_dir, "power_plants.npz")
    if sha256(npz) != m["files"][0]["sha256"] or os.path.getsize(npz) != m["files"][0]["size"]:
        bad.append("the npz is not the manifest's")
    with np.load(npz, allow_pickle=False) as z:
        d = {k: np.array(z[k]) for k in z.files}
    for key, prefix in (("p_lat", "p_"), ("s_lat", "s_")):     # (the server's reader rules: powerplants.validate)
        rows = len(d[key])
        if any(len(v) != rows for c, v in d.items() if c.startswith(prefix) and c != "p_name_off") \
                or (np.diff(d[key]) < 0).any():
            bad.append(f"{prefix} columns: lengths or latitude order")
    if len(d["p_name_off"]) != len(d["p_lat"]) + 1 or int(d["p_name_off"][-1]) != len(d["names"]):
        bad.append("names")
    n, k = len(d["p_lat"]), len(d["s_lat"])
    if n != m["plants"] or k != m["structures"]:
        bad.append("counts differ from the manifest's")
    names = d["names"].tobytes()
    off = d["p_name_off"]
    lat, lon = d["p_lat"] / 1e6, d["p_lon"] / 1e6
    for word, la, lo, fuel, towers in ANCHORS:
        near = np.flatnonzero((np.abs(lat - la) < 0.05) & (np.abs(lon - lo) < 0.08))
        hit = [i for i in near if word in names[off[i]:off[i + 1]].decode("utf-8", "replace").lower()]
        if not hit:
            bad.append(f"anchor {word}: missing")
            continue
        i = hit[0]
        if FUELS[int(d["p_fuel"][i])] != fuel:
            bad.append(f"anchor {word}: fuel {FUELS[int(d['p_fuel'][i])]}, not {fuel}")
        got = int(((d["s_plant"] == i) & (d["s_kind"] == KIND_COOLING)).sum())
        if got < towers:
            bad.append(f"anchor {word}: {got} mapped cooling towers, fewer than {towers}")
    if previous:
        with open(previous, encoding="utf-8") as f:
            pm = json.load(f)
        for key in ("plants", "structures"):
            r = m[key] / max(1, pm[key])
            if not GROWTH[0] <= r <= GROWTH[1]:
                bad.append(f"{key}: {m[key]} against the published {pm[key]} ({r:.3f}x)")
    for b in bad:
        print("REFUSED:", b)
    if bad:
        sys.exit(1)
    print(f"ok: {n} plants, {k} structures, every anchor")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("fetch-osm", "stage", "validate"):
        ap = argparse.ArgumentParser(description="the weekly data-repo build")
        sub = ap.add_subparsers(dest="cmd", required=True)
        p_ = sub.add_parser("fetch-osm")
        p_.add_argument("out_dir")
        p_ = sub.add_parser("stage")
        p_.add_argument("--inputs", required=True)
        p_.add_argument("--osm", required=True)
        p_.add_argument("--out", required=True)
        p_.add_argument("--version", required=True)
        p_.add_argument("--fetch", action="store_true")
        p_ = sub.add_parser("validate")
        p_.add_argument("out_dir")
        p_.add_argument("--previous")
        a = ap.parse_args()
        if a.cmd == "fetch-osm":
            fetch_osm(a.out_dir)
        elif a.cmd == "stage":
            if not re.fullmatch(r"v[1-9][0-9]{0,3}", a.version):
                sys.exit("bad version")
            if a.fetch:
                fetch(a.inputs)
            print(json.dumps(stage(a.inputs, a.osm, a.out, a.version)))
        else:
            validate(a.out_dir, a.previous)
        sys.exit(0)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--osm", required=True)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    if a.fetch:
        fetch(a.inputs)
    build(a.inputs, a.osm, a.out)
