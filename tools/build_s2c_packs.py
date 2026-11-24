#!/usr/bin/env python3
"""BeamNG.Earth's own cloud-free Sentinel-2 composite of 2025 ("s2c"; the user, 2026-11-23: "Go with the 2025"), as
tile packs (server/tilepack.py) for the project's tile host (imagery source "bngs2c", CDN prefix s2c/v1): the successor
of the WorldCover 2021 packs (build_s2_packs.py, s2wc/v1), drawn ahead of them; black is "no data" in both (the sea).

Source: Element84 Earth Search, STAC collection sentinel-2-c1-l2a (AWS Open Data, s3://e84-earth-search-sentinel-data,
us-west-2; anonymous HTTPS range reads of Cloud-Optimised GeoTIFFs). Licence: Copernicus Sentinel data terms (free, full
and open, any use, redistribution; credit "Contains modified Copernicus Sentinel data 2024-2025"). No account, no key.
The c1 collection (not sentinel-2-l2a: that one applies the -1000 DN offset of baseline 04.00+ twice in places).

Method (each step deterministic: same inputs, same bytes on any machine; see "Determinism" below):
  1. Scenes per MGRS tile, not per pack (packs that share a tile share its scenes: no colour steps between packs).
     A tile's items are listed by its grid code (STAC), grouped by relative orbit (an orbit sees its own part of a
     tile at a swath edge) and ranked by the item's own whole-tile metadata only (no pixels): clear = 1 - (cloud +
     cloud shadow) %, rank = clear - snow/ice % (ties: date, id). Per orbit, scenes are added in rank order until the
     clear shares sum to TARGET_CLEAR (expected clear looks per pixel) or MAX_PER_ORBIT; at least MIN_PER_ORBIT when
     that many exist. Items under MIN_ITEM_CLEAR or MIN_ITEM_DATA (the orbit's sliver) are never taken.
  2. Season (SEASONS, by the tile's latitude band, the hemisphere's summer): stages tried in order, the next stage's
     scenes appended only while an orbit is short of TARGET_CLEAR:
        |lat| >= 50 (bands U..X / C..E): Jun-Aug 2025 (Dec 2024-Feb 2025 south), then May-Sep, then all 2025, then
                                         2024-2025
        23.5 <= |lat| < 50:              May-Sep 2025 (Nov 2024-Mar 2025 south), then Apr-Oct, then all 2025, then
                                         2024-2025
        tropics (bands M..Q):            all 2025, then 2024-2025 (wet seasons differ by region: the year as a whole)
     Leaf-on, low snow, highest sun (fewest terrain shadows); the fallback stages only where clouds or snow leave an
     orbit short (monsoon Asia, the tropics' wet coasts, Patagonia, the Arctic).
  3. Per scene, per pack pixel: the bands B04 / B03 / B02 (10 m) bilinear at the pixel centre, reflectance x 10^4 =
     DN - 1000 (c1: offset -0.1 on every item, baseline 05.x); SCL (20 m) nearest. Masked: SCL 0 / 1 / 3 / 8 / 9 / 10
     (no data, defective, cloud shadow, cloud medium / high, cirrus), grown GROW_PX (100 m) on the SCL's own grid (with
     its margin: the same mask whatever pack reads it); snow / ice (SCL 11) kept apart.
  4. Per tile and pixel, per band: the 40 % quantile of the clear looks (COMPOSITE "q40"); only snow looks left:
     those (glaciers stay white); nothing clear: the darker half of every look (cloud fringes are bright). Compared
     2026-11-23 on 11 places (8 with open aerials; ~/bnge-tests/s2c): with the fitted tone the median, q40 and q30
     fit the aerials' colours alike (dE of 16 px means 10.9 / 10.9 / 11.0), q40 best over all 11 (10.6 / 10.8) and
     slightly better in structure (correlation with the aerial +0.005: less haze, fewer bright cloud fringes); the
     medoid gave nothing for 1.07x the speckle.
  5. Tiles blended where they overlap (the 9.8 km MGRS overlap, wider at UTM zone edges): weight = distance into the
     tile (metres) / FEATHER_M, capped at 1 (no seam where one tile's scene set meets the next).
  6. Tone (TONE: reflectance -> sRGB-ish 8 bit, a LUT; saturation round the luminance; fitted to the aerials: dE
     18.2 -> 10.9, L* 65 -> 48 against their 50), then two unsharp masks of the luminance on the photo zoom: SHARPEN
     (3 x 3) and CLARITY (9 x 9 box, local contrast): sharpness (mean |grad L*|) 5.8 -> ~11.5 against EOX 2023's ~14
     and the aerials' ~17 at the same zoom, without the dark rims round bright roofs that 12 / 24 gave. Every data
     pixel at least DATA_MIN (black stays "no data": the chain's next source, as bngs2's open sea; the map's sea fill
     still finds it).
  7. WebP q80 tiles in packs of 8 x 8 (k = 3).

Zooms: the photo zoom is 14 within --max-lat (50 degrees: bngs2's z14 limit) and 13 beyond; every coarser zoom down to
11 is the 2 x 2 mean of the one below (from the un-encoded pixels, in the shard); 10..8 by --coarse from the zoom-11
packs (a later stage, per zoom-5 region), 7..3 by --world from the zoom-8 packs (the last stage). A shard is one
zoom-8 tile: its 64 zoom-14 packs (zoom-11 tiles), 16 zoom-13, 4 zoom-12 and 1 zoom-11 pack.

    build_s2c_packs.py OUT --shards "136,91 137,91"            # zoom-8 tiles (x,y), any machine; resumable
    build_s2c_packs.py OUT --shard-file list.txt --workers 4 --deadline T     # (one "x,y" a line)
    build_s2c_packs.py OUT --bbox 46.0 10.0 47.0 11.0           # the shards over a box (a local test)
    build_s2c_packs.py OUT --coarse 16,11 --from DIR             # zooms 10..8 of zoom-5 region 16,11 from DIR's z11
    build_s2c_packs.py OUT --world --from https://tiles.tgforever.net/s2c/v1    # zooms 7..3 from the zoom-8 packs
    build_s2c_packs.py OUT --pack 14 8662 5789                   # one photo pack, no record (a sheet)
    build_s2c_packs.py x --selftest                              # the pipeline's SHA-256 (compare with the pins)
    build_s2c_packs.py tools/s2c_shards.txt --list-planet        # the planet's shards (WorldCover's land cells)

Writes OUT/{z}/{px}/{py}.bngp and per shard OUT/_build/s2c/v1/shards/{x}_{y}.json (its packs' sizes and SHA-256, the
scenes of each MGRS tile, bytes read, seconds), the record last. A shard with a record is skipped (OUT or --skip: a
listing of what the bucket has). Exit 75 when --deadline left shards for a later run.

Determinism: no GDAL. The COGs are read by this file (TIFF tile tables, zlib, predictor 2: integers). The Web-Mercator
-> UTM mapping uses only + - * / and sqrt (IEEE-754 exact on every CPU) with this file's own exp / log / sin / cos /
atan (polynomials with fixed range reduction, ~1e-15), on a node grid every NODE_PX pixels; source positions are
fixed point (1/64 pixel) from there on; bilinear, quantiles, blend, tone (a table made by the same functions) and
sharpening are integer arithmetic. Scenes are chosen from STAC metadata alone, in a fixed order; threads and processes
only change when a pack is made, never its pixels. Only Pillow's WebP encoder (libwebp) is outside: the pinned Pillow
(PILLOW_PIN; the record names the version, the checker refuses another) and --selftest's SHA-256 (a synthetic pack
through the whole pixel pipeline, no network) checked on a machine before it builds shards.
"""
import argparse
import hashlib
import http.client
import io
import json
import math
import os
import re
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

try:        # (first: numpy's AVX-512 kernels held back before anything imports numpy, tools/npyfirst.py)
    import npyfirst  # noqa: E402,F401
except ImportError:     # (the data repo's copy: no npyfirst there; nothing below depends on numpy's dispatch)
    pass
import numpy as np
import PIL
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import tilepack  # noqa: E402

FORMAT = 1                  # the record's format
PREFIX = "s2c/v1"
STAC = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-c1-l2a"
DATA_HOST = "e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com"
DATA_PATH = "/sentinel-2-c1-l2a/{zone}/{band}/{sq}/{year}/{month}/{id}/{asset}.tif"
UA = "BeamNG.Earth tile builder"
PILLOW_PIN = "13.0.0"       # (informational here; the workflow installs this one)
K = 3
TILE = 256
PACK_PX = TILE << K         # 2048
SHARD_Z = 8
MAX_LAT_14 = 50.0           # the photo zoom is 14 within this latitude (bngs2's z14 packs: tiles.yml max_lat 50)
R = 6378137.0
SPAN = 2 * math.pi * R
# scene choice (per MGRS tile and relative orbit; STAC metadata only)
TARGET_CLEAR = 6.0          # expected clear looks per pixel
MIN_PER_ORBIT = 4
MAX_PER_ORBIT = 10
MIN_ITEM_CLEAR = 0.15       # an item under 15 % clear: never taken
MIN_ITEM_DATA = 2.0         # % of the tile with data: under this the item (an orbit's sliver) is never taken
MAX_CLOUD = 85              # (the STAC query's own filter)
# pixels
BAD = (0, 1, 3, 8, 9, 10)
SNOW = 11
GROW_PX = 5                 # the cloud mask grown 5 SCL pixels (100 m)
MIN_CLEAR = 2               # clear looks for the clear median; fewer: see composite()
FEATHER_M = 5000.0
OFFSET_DN = 1000            # c1: reflectance x 10^4 = DN - 1000 (raster:bands offset -0.1, scale 1e-4)
DATA_MIN = 8                # a data pixel's brightest channel (imagery.NODATA_MAX 6: black is "no data")
NODE_PX = 16                # the projection's node grid (pixels)
QB = 6                      # fixed point: 1/64 pixel
COMPOSITE_CHUNK = 1 << 19   # pixels composited at once
COMPOSITE = "q40"           # (per band, the 40 % quantile of the clear looks; the comparison: README of the commit)
# reflectance x 10^4 -> 8 bit: ((r - black) / (white - black)) ^ (1 / gamma), then saturation round the luminance.
# bngs2 (WorldCover 2021) used white 3000, gamma 2.0, sat 1.2: pale and low in contrast next to the aerials (mean L* 65
# against their 50, dE of 16 px means 18.2 over 8 places with open aerials, 2026-11-23); this one, fitted on the same
# places over 192 curves x 3 composites: dE 10.9, L* 48.
TONE = {"black": 0.0, "white": 5000.0, "gamma": 1.8, "sat": 0.9}
SHARPEN = 8                 # unsharp mask amount / 16 on the photo zoom (0: off): 3 x 3 binomial
CLARITY = 16                # ... and a broader one ("clarity", local contrast): a (2 CLARITY_R + 1)^2 box
CLARITY_R = 4
WEBP_Q = 80
WEBP_METHOD = 4
_LOCAL = threading.local()
_lock = threading.Lock()
STATS = {"bytes": 0, "requests": 0, "seconds": 0.0}

# ---------------------------------------------------------------------------------------------------------------------
# Deterministic float64 functions: + - * / and sqrt only (IEEE-754 exact rounding on every CPU), fixed range reduction.
# numpy's own exp / sin / ... use the platform's libm or SIMD code (SVML on AVX-512), which differ in the last bits.
LN2_HI, LN2_LO = 6.93147180369123816490e-01, 1.90821492927058770002e-10
_INV_LN2 = 1.4426950408889634


def _exp(x):
    x = np.asarray(x, np.float64)
    k = np.floor(x * _INV_LN2 + 0.5)
    r = (x - k * LN2_HI) - k * LN2_LO                  # |r| <= ln2/2
    # exp(r) = (exp(r/8))^8, Taylor to r^10 of r/8 (|r/8| < 0.044: error < 1e-17)
    s = r * 0.125
    p = 1.0 + s * (1.0 + s * (1 / 2 + s * (1 / 6 + s * (1 / 24 + s * (1 / 120 + s * (1 / 720 + s * (1 / 5040 + s * (
        1 / 40320 + s * (1 / 362880 + s * (1 / 3628800))))))))))
    p = p * p
    p = p * p
    p = p * p
    return p * _pow2(k)


def _pow2(k):
    """2^k for integer-valued float64 k (|k| < 1000), exactly."""
    k = np.asarray(k, np.int64)
    return ((k + 1023).astype(np.uint64) << np.uint64(52)).view(np.float64)


def _log(x):
    """Natural log for x > 0."""
    x = np.asarray(x, np.float64)
    bits = x.view(np.uint64)
    e = ((bits >> np.uint64(52)) & np.uint64(0x7FF)).astype(np.int64) - 1023
    m = ((bits & np.uint64(0x000FFFFFFFFFFFFF)) | np.uint64(0x3FF0000000000000)).view(np.float64)   # [1, 2)
    big = m > 1.4142135623730951
    m = np.where(big, m * 0.5, m)
    e = e + big
    t = (m - 1.0) / (m + 1.0)                          # |t| < 0.1716
    t2 = t * t
    s = t2 * (1 / 3 + t2 * (1 / 5 + t2 * (1 / 7 + t2 * (1 / 9 + t2 * (1 / 11 + t2 * (1 / 13 + t2 * (1 / 15 + t2 * (
        1 / 17 + t2 * (1 / 19 + t2 * (1 / 21 + t2 * (1 / 23)))))))))))
    lm = 2.0 * t + 2.0 * t * s
    ef = e.astype(np.float64)
    return ef * LN2_HI + (ef * LN2_LO + lm)


_PIO2_HI, _PIO2_LO = 1.57079632673412561417e+00, 6.07710050650619224932e-11


def _sincos(x):
    x = np.asarray(x, np.float64)
    k = np.floor(x * (2 / math.pi) + 0.5)
    r = (x - k * _PIO2_HI) - k * _PIO2_LO               # |r| <= pi/4 (|x| < 1e5: exact enough)
    r2 = r * r
    s = r * (1.0 + r2 * (-1 / 6 + r2 * (1 / 120 + r2 * (-1 / 5040 + r2 * (1 / 362880 + r2 * (-1 / 39916800 + r2 * (
        1 / 6227020800 + r2 * (-1 / 1307674368000))))))))
    c = 1.0 + r2 * (-1 / 2 + r2 * (1 / 24 + r2 * (-1 / 720 + r2 * (1 / 40320 + r2 * (-1 / 3628800 + r2 * (
        1 / 479001600 + r2 * (-1 / 87178291200 + r2 * (1 / 20922789888000))))))))
    q = np.asarray(k, np.int64) & 3
    sin = np.select([q == 0, q == 1, q == 2], [s, c, -s], -c)
    cos = np.select([q == 0, q == 1, q == 2], [c, -s, -c], s)
    return sin, cos


def _atan(x):
    x = np.asarray(x, np.float64)
    neg = x < 0
    a = np.abs(x)
    inv = a > 1.0
    a = np.where(inv, 1.0 / np.where(a == 0, 1.0, a), a)     # [0, 1]
    # reduce twice by atan(a) = 2 atan(a / (1 + sqrt(1 + a^2))): |a| <= tan(pi/16) ~ 0.199
    a = a / (1.0 + np.sqrt(1.0 + a * a))
    a = a / (1.0 + np.sqrt(1.0 + a * a))
    a2 = a * a
    s = np.zeros_like(a)
    for n in range(14, -1, -1):                         # sum_n (-1)^n a^2n / (2n + 1): 0.04^15 / 31 ~ 1e-23
        s = (-1.0 if n % 2 else 1.0) / (2 * n + 1) + a2 * s
    r = 4.0 * a * s
    r = np.where(inv, math.pi / 2 - r, r)
    return np.where(neg, -r, r)


def _atan2(y, x):
    """atan2 for x > 0 (all this file needs: cos(lambda) > 0 within a UTM zone's reach)."""
    return _atan(y / x)


def _sinh(x):
    e = _exp(x)
    return 0.5 * (e - 1.0 / e)


def _cosh(x):
    e = _exp(x)
    return 0.5 * (e + 1.0 / e)


def _asinh(x):
    x = np.asarray(x, np.float64)
    a = np.abs(x)
    r = _log(a + np.sqrt(a * a + 1.0))                  # (absolute error ~1e-16 near 0: ~1 nm of easting)
    return np.where(x < 0, -r, r)


def _atanh(x):
    x = np.asarray(x, np.float64)
    return 0.5 * _log((1.0 + x) / (1.0 - x))


# WGS84 / UTM (Krueger series, n^6: ~1 mm)
_A, _F = 6378137.0, 1 / 298.257223563
_E = math.sqrt(_F * (2 - _F))
_N = _F / (2 - _F)
_AA = _A / (1 + _N) * (1 + _N ** 2 / 4 + _N ** 4 / 64 + _N ** 6 / 256)
_ALPHA = (
    _N / 2 - 2 * _N ** 2 / 3 + 5 * _N ** 3 / 16 + 41 * _N ** 4 / 180 - 127 * _N ** 5 / 288 + 7891 * _N ** 6 / 37800,
    13 * _N ** 2 / 48 - 3 * _N ** 3 / 5 + 557 * _N ** 4 / 1440 + 281 * _N ** 5 / 630 - 1983433 * _N ** 6 / 1935360,
    61 * _N ** 3 / 240 - 103 * _N ** 4 / 140 + 15061 * _N ** 5 / 26880 + 167603 * _N ** 6 / 181440,
    49561 * _N ** 4 / 161280 - 179 * _N ** 5 / 168 + 6601661 * _N ** 6 / 7257600,
    34729 * _N ** 5 / 80640 - 3418889 * _N ** 6 / 1995840,
    212378941 * _N ** 6 / 319334400)
_K0 = 0.9996


def merc_to_utm(mx, my, zone, south):
    """EPSG:3857 metres (mx, my arrays) -> UTM zone easting, northing (float64), deterministic."""
    tau = _sinh(np.asarray(my, np.float64) / R)          # tan(latitude): Web Mercator's y is the conformal one
    lam = np.asarray(mx, np.float64) / R - math.radians(zone * 6 - 183)
    sig = _sinh(_E * _atanh(_E * tau / np.sqrt(1.0 + tau * tau)))
    taup = tau * np.sqrt(1.0 + sig * sig) - sig * np.sqrt(1.0 + tau * tau)
    sl, cl = _sincos(lam)
    xip = _atan2(taup, cl)
    etap = _asinh(sl / np.sqrt(taup * taup + cl * cl))
    s1, c1 = _sincos(2.0 * xip)
    e1 = _exp(2.0 * etap)
    sh1, ch1 = 0.5 * (e1 - 1.0 / e1), 0.5 * (e1 + 1.0 / e1)
    xi, eta = xip, etap
    # sin / cos (2j xi'), sinh / cosh (2j eta') by the angle sum recurrences (no more transcendental calls)
    sj, cj, shj, chj = s1, c1, sh1, ch1
    for j, a in enumerate(_ALPHA, 1):
        xi = xi + a * sj * chj
        eta = eta + a * cj * shj
        sj, cj = sj * c1 + cj * s1, cj * c1 - sj * s1
        shj, chj = shj * ch1 + chj * sh1, chj * ch1 + shj * sh1
    e = 500000.0 + _K0 * _AA * eta
    n = _K0 * _AA * xi + (10000000.0 if south else 0.0)
    return e, n


# ---------------------------------------------------------------------------------------------------------------------
# HTTP (one persistent connection per thread and host) and the COG reader


def _conn(host):
    cs = getattr(_LOCAL, "conns", None)
    if cs is None:
        cs = _LOCAL.conns = {}
    c = cs.get(host)
    if c is None:
        c = cs[host] = http.client.HTTPSConnection(host, timeout=60, context=ssl.create_default_context())
    return c


def get_range(host, path, start, end):
    """Bytes [start, end) of https://host/path; None on 404."""
    t0 = time.time()
    for attempt in range(6):
        c = _conn(host)
        try:
            c.request("GET", path, headers={"User-Agent": UA, "Range": f"bytes={start}-{end - 1}"})
            r = c.getresponse()
            body = r.read()
            if r.status == 404:
                return None
            if r.status not in (200, 206):
                raise IOError(f"HTTP {r.status}")
            if r.status == 200:
                body = body[start:end]
            if len(body) != end - start:
                # (a range past the file's end: the server gives what there is, Content-Range ".../total")
                total = (r.getheader("Content-Range") or "").rpartition("/")[2]
                if not (total.isdigit() and start + len(body) == int(total) and end > int(total)):
                    raise IOError(f"short read {len(body)} of {end - start}")
            with _lock:
                STATS["bytes"] += len(body)
                STATS["requests"] += 1
                STATS["seconds"] += time.time() - t0
            return body
        except (OSError, http.client.HTTPException) as e:
            try:
                c.close()
            except Exception:       # noqa: BLE001
                pass
            _LOCAL.conns.pop(host, None)
            if attempt == 5:
                raise IOError(f"{host}{path} {start}-{end}: {e}") from None
            time.sleep(min(30.0, 1.0 * 2 ** attempt))


class Cog:
    """One single-band COG: its full-resolution level's tile table (offsets, counts) and layout."""

    def __init__(self, host, path):
        self.host, self.path = host, path
        head = get_range(host, path, 0, 1 << 16)
        if head is None:
            raise FileNotFoundError(path)
        big = struct.unpack_from("<H", head, 2)[0] == 43
        off = struct.unpack_from("<Q", head, 8)[0] if big else struct.unpack_from("<I", head, 4)[0]
        tags = self._ifd(head, off, big)
        self.w, self.h = tags[256][0], tags[257][0]
        self.tw, self.th = tags[322][0], tags[323][0]
        self.offs, self.cnts = tags[324], tags[325]
        self.bits = tags[258][0]
        self.pred = tags.get(317, (1,))[0]
        comp = tags.get(259, (1,))[0]
        if comp != 8 or self.bits not in (8, 16) or tags.get(277, (1,))[0] != 1:
            raise ValueError(f"{path}: compression {comp}, {self.bits} bits: not a deflate single-band COG")
        self.nx = -(-self.w // self.tw)
        # the georeference: ModelTiepoint (raster 0, 0 -> x, y) and ModelPixelScale
        tp, ps = tags.get(33922), tags.get(33550)
        self.origin = (float(tp[3]), float(tp[4])) if tp and tp[0] == 0 and tp[1] == 0 else None
        self.pixel = float(ps[0]) if ps else None

    def _ifd(self, b, off, big):
        types = {3: ("H", 2), 4: ("I", 4), 16: ("Q", 8), 12: ("d", 8)}
        if big:
            n = struct.unpack_from("<Q", b, off)[0]
            p, esz = off + 8, 20
        else:
            n = struct.unpack_from("<H", b, off)[0]
            p, esz = off + 2, 12
        tags = {}
        for i in range(n):
            e = p + i * esz
            tag, typ = struct.unpack_from("<HH", b, e)
            cnt = struct.unpack_from("<Q" if big else "<I", b, e + 4)[0]
            if typ not in types:
                continue
            f, sz = types[typ]
            voff = e + (12 if big else 8)
            if cnt * sz > (8 if big else 4):
                voff = struct.unpack_from("<Q" if big else "<I", b, voff)[0]
                if voff + cnt * sz > len(b):
                    data = get_range(self.host, self.path, voff, voff + cnt * sz)
                    tags[tag] = struct.unpack_from(f"<{cnt}{f}", data, 0)
                    continue
            tags[tag] = struct.unpack_from(f"<{cnt}{f}", b, voff)
        return tags

    def _decode(self, raw):
        a = np.frombuffer(zlib.decompress(raw), "<u2" if self.bits == 16 else "u1").reshape(self.th, self.tw)
        if self.pred == 2:
            a = np.cumsum(a, axis=1, dtype=a.dtype)        # (wraps modulo 2^bits: the predictor's arithmetic)
        return a

    def start(self, r0, r1, c0, c1):
        """Begin reading rows [r0, r1) x cols [c0, c1): the blocks' byte ranges, merged where they lie next to each
        other in the file (gap < MERGE_GAP), submitted to the process's fetch pool. finish() assembles them."""
        want = []
        for by in range(max(0, r0) // self.th, (min(self.h, r1) - 1) // self.th + 1):
            for bx in range(max(0, c0) // self.tw, (min(self.w, c1) - 1) // self.tw + 1):
                i = by * self.nx + bx
                if self.cnts[i]:
                    want.append((self.offs[i], self.cnts[i], bx, by))
        want.sort()
        runs = []
        for w in want:
            if runs and w[0] - (runs[-1][-1][0] + runs[-1][-1][1]) < MERGE_GAP and \
                    w[0] + w[1] - runs[-1][0][0] <= MERGE_MAX:
                runs[-1].append(w)
            else:
                runs.append([w])
        futs = [(run, fetch_pool().submit(get_range, self.host, self.path, run[0][0], run[-1][0] + run[-1][1]))
                for run in runs]
        return (r0, r1, c0, c1, futs)

    def finish(self, handle):
        """The window start() began (zeros outside the image and in sparse blocks)."""
        r0, r1, c0, c1, futs = handle
        out = np.zeros((r1 - r0, c1 - c0), np.uint16 if self.bits == 16 else np.uint8)
        for run, f in futs:
            data = f.result()
            base = run[0][0]
            for o, n, bx, by in run:
                b = self._decode(data[o - base:o - base + n])
                g0, g1 = max(r0, by * self.th), min(r1, (by + 1) * self.th, self.h)
                h0, h1 = max(c0, bx * self.tw), min(c1, (bx + 1) * self.tw, self.w)
                if g0 < g1 and h0 < h1:
                    out[g0 - r0:g1 - r0, h0 - c0:h1 - c0] = b[g0 - by * self.th:g1 - by * self.th,
                                                              h0 - bx * self.tw:h1 - bx * self.tw]
        return out

    def window(self, r0, r1, c0, c1):
        return self.finish(self.start(r0, r1, c0, c1))


SCENE_THREADS = 4           # scenes decoded at once per pack (CPU: zlib, gathers)
HEAD_THREADS = 16          # COG headers fetched at once
MERGE_GAP = 1 << 16
MERGE_MAX = 16 << 20
FETCH_THREADS = 24
_POOL = None


def fetch_pool():
    global _POOL
    if _POOL is None:
        with _lock:
            if _POOL is None:
                _POOL = ThreadPoolExecutor(FETCH_THREADS, thread_name_prefix="fetch")
    return _POOL


_cogs = OrderedDict()


def cog(item_id, asset):
    key = (item_id, asset)
    with _lock:
        c = _cogs.get(key)
        if c is not None:
            _cogs.move_to_end(key)
            return c
    c = Cog(DATA_HOST, asset_path(item_id, asset))
    with _lock:
        _cogs[key] = c
        while len(_cogs) > 512:
            _cogs.popitem(last=False)
    return c


ID_RE = re.compile(r"(S2[ABCD])_T(\d{2})([C-X])([A-Z]{2})_(\d{4})(\d{2})(\d{2})T\d{6}_L2A")


def asset_path(item_id, asset):
    """The S3 key of an item's asset (Earth Search's layout: zone / band / square / year / month (no leading zero))."""
    m = ID_RE.fullmatch(item_id)
    if not m:
        raise ValueError(f"item id {item_id!r}")
    return DATA_PATH.format(zone=int(m.group(2)), band=m.group(3), sq=m.group(4), year=m.group(5),
                            month=int(m.group(6)), id=item_id, asset=asset)


# ---------------------------------------------------------------------------------------------------------------------
# STAC: the MGRS tiles over a box, a tile's items, its scenes


def stac_post(body):
    data = json.dumps(body).encode()
    for attempt in range(6):
        try:
            req = urllib.request.Request(STAC, data=data,
                                         headers={"User-Agent": UA, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except Exception as e:          # noqa: BLE001
            if attempt == 5:
                raise IOError(f"STAC: {e}") from None
            time.sleep(min(60.0, 2.0 * 2 ** attempt))


def stac_all(body):
    out, seen = [], set()
    body = dict(body, limit=250)
    for _ in range(100):
        doc = stac_post(body)
        feats = doc.get("features") or []
        for f in feats:
            if f["id"] not in seen:
                seen.add(f["id"])
                out.append(f)
        nxt = [ln for ln in doc.get("links", []) if ln.get("rel") == "next" and ln.get("body")]
        if len(feats) < body["limit"] or not nxt:
            break
        body = nxt[0]["body"]
    return out


ITEM_FIELDS = {"include": ["id", "properties.datetime", "properties.eo:cloud_cover",
                           "properties.s2:nodata_pixel_percentage",
                           "properties.s2:cloud_shadow_percentage", "properties.s2:snow_ice_percentage",
                           "properties.s2:product_uri", "properties.s2:processing_baseline", "properties.proj:epsg",
                           "properties.grid:code"],
               "exclude": ["assets", "links", "geometry", "bbox"]}
CODE_RE = re.compile(r"MGRS-(\d{2})([C-X])([A-Z]{2})")


def tiles_over(box_ll, cache=None):
    """The MGRS tiles (grid codes, sorted) with items over (lat_s, lon_w, lat_n, lon_e): every tile that has any item
    in two months of either hemisphere's summer whose footprint meets the box (a superset of what any pixel uses; each
    pixel takes only the tiles whose square holds it)."""
    s, w, n, e = box_ll
    key = f"tiles_{s:.6f}_{w:.6f}_{n:.6f}_{e:.6f}"
    got = _cache_get(cache, key)
    if got is not None:
        return got
    codes = set()
    for when in ("2025-06-01T00:00:00Z/2025-07-31T23:59:59Z", "2024-12-01T00:00:00Z/2025-01-31T23:59:59Z"):
        for f in stac_all({"collections": [COLLECTION], "bbox": [w, s, e, n], "datetime": when,
                           "fields": {"include": ["id", "properties.grid:code"],
                                      "exclude": ["assets", "links", "geometry", "bbox"]}}):
            c = (f.get("properties") or {}).get("grid:code")
            if c and CODE_RE.fullmatch(c):
                codes.add(c)
    out = sorted(codes)
    _cache_put(cache, key, out)
    return out


def _cache_get(cache, key):
    if not cache:
        return None
    p = os.path.join(cache, key + ".json")
    try:
        with open(p) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _cache_put(cache, key, v):
    if not cache:
        return
    os.makedirs(cache, exist_ok=True)
    p = os.path.join(cache, key + ".json")
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(v, fh, sort_keys=True)
    os.replace(tmp, p)


def band_lat(band):
    """The latitude band's centre (degrees)."""
    letters = "CDEFGHJKLMNPQRSTUVWX"
    i = letters.index(band)
    return -80 + 8 * i + (6 if band == "X" else 4)


def seasons(lat):
    """The STAC datetime stages of a tile at latitude lat (its band's centre), in order (see the docstring)."""
    y = 2025
    every = (f"{y}-01-01T00:00:00Z/{y}-12-31T23:59:59Z", f"{y - 1}-01-01T00:00:00Z/{y}-12-31T23:59:59Z")
    if abs(lat) < 23.5:
        return every
    north = lat > 0
    if abs(lat) >= 50:
        core = (f"{y}-06-01T00:00:00Z/{y}-08-31T23:59:59Z" if north else f"{y - 1}-12-01T00:00:00Z/{y}-02-28T23:59:59Z",
                f"{y}-05-01T00:00:00Z/{y}-09-30T23:59:59Z" if north else f"{y - 1}-11-01T00:00:00Z/{y}-03-31T23:59:59Z")
    else:
        core = (f"{y}-05-01T00:00:00Z/{y}-09-30T23:59:59Z" if north else f"{y - 1}-11-01T00:00:00Z/{y}-03-31T23:59:59Z",
                f"{y}-04-01T00:00:00Z/{y}-10-31T23:59:59Z" if north else f"{y - 1}-10-01T00:00:00Z/{y}-04-30T23:59:59Z")
    return core + every


def _meta(f):
    p = f["properties"]
    cloud = float(p.get("eo:cloud_cover") or 0.0)
    shadow = float(p.get("s2:cloud_shadow_percentage") or 0.0)
    snow = float(p.get("s2:snow_ice_percentage") or 0.0)
    data = 100.0 - float(p.get("s2:nodata_pixel_percentage") or 0.0)
    m = re.search(r"_R(\d{3})_", p.get("s2:product_uri") or "")
    clear = max(0.0, 1.0 - (cloud + shadow) / 100.0)
    return {"id": f["id"], "dt": p.get("datetime", ""), "orbit": m.group(1) if m else "?",
            "clear": round(clear, 6), "rank": round(clear - snow / 100.0, 6), "data": round(data, 4),
            "baseline": p.get("s2:processing_baseline") or "", "epsg": int(p.get("proj:epsg") or 0)}


def tile_scenes(code, cache=None):
    """The scenes of MGRS tile code (grid code "MGRS-32TNS"): {"epsg", "ulx", "uly", "scenes": [{id, orbit, clear}...]}
    in the order they are composited, and the stages used; deterministic from the STAC metadata (see the docstring)."""
    got = _cache_get(cache, "scenes_" + code)
    if got is not None:
        return got
    m = CODE_RE.fullmatch(code)
    zone, band = int(m.group(1)), m.group(2)
    stages = seasons(band_lat(band))
    chosen, have = {}, {}           # orbit -> [meta], orbit -> clear sum
    seen = set()
    used = 0
    epsg = 0
    for si, when in enumerate(stages):
        if si > 0 and have and all(have[o] >= TARGET_CLEAR or len(chosen[o]) >= MAX_PER_ORBIT for o in have):
            break
        items = stac_all({"collections": [COLLECTION], "datetime": when, "fields": ITEM_FIELDS,
                          "query": {"grid:code": {"eq": code}, "eo:cloud_cover": {"lt": MAX_CLOUD}}})
        used = si + 1
        metas = [_meta(f) for f in items if f["id"] not in seen and ID_RE.fullmatch(f["id"])]
        seen.update(f["id"] for f in items)
        by_orbit = {}
        for mt in metas:
            if mt["baseline"] and mt["baseline"] < "04.00":
                continue                # (c1 is all 05.x; an older baseline would need offset 0)
            if mt["clear"] < MIN_ITEM_CLEAR or mt["data"] < MIN_ITEM_DATA:
                continue
            epsg = epsg or mt["epsg"]
            by_orbit.setdefault(mt["orbit"], []).append(mt)
        for o in sorted(by_orbit):
            lst = sorted(by_orbit[o], key=lambda t: (-t["rank"], t["dt"], t["id"]))
            ch = chosen.setdefault(o, [])
            hv = have.get(o, 0.0)
            for mt in lst:
                if (hv >= TARGET_CLEAR and len(ch) >= MIN_PER_ORBIT) or len(ch) >= MAX_PER_ORBIT:
                    break
                ch.append(mt)
                hv += mt["clear"]
            have[o] = hv
    scenes = [mt for o in sorted(chosen) for mt in chosen[o]]
    south = band < "N"
    if not epsg:
        epsg = (32700 if south else 32600) + zone
    ulx, uly = _tile_origin(code)
    out = {"code": code, "epsg": epsg, "ulx": ulx, "uly": uly, "stages": used,
           "scenes": [{"id": s["id"], "orbit": s["orbit"], "clear": s["clear"], "dt": s["dt"][:10]} for s in scenes],
           "clear_sum": {o: round(v, 3) for o, v in sorted(have.items())}}
    _cache_put(cache, "scenes_" + code, out)
    return out


_COLS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_ROWS = "ABCDEFGHJKLMNPQRSTUV"


def _tile_origin(code):
    """The S2 tile's upper-left corner (UTM metres) from its MGRS code (a check: the builder takes each COG's own
    tiepoint): the 100 km square's west edge floored to 60 m, its north edge raised to 60 m (from the false northing
    in the south); the tiles are 109.8 km (1830 x 60 m) from there, 9.8 km past the square east and south."""
    m = CODE_RE.fullmatch(code)
    zone, band, sq = int(m.group(1)), m.group(2), m.group(3)
    col = _COLS.index(sq[0]) - 8 * ((zone - 1) % 3)
    e0 = (col + 1) * 100000
    row = (_ROWS.index(sq[1]) - (5 if zone % 2 == 0 else 0)) % 20
    south = band < "N"
    lat0 = -80 + 8 * "CDEFGHJKLMNPQRSTUVWX".index(band)
    # the band's southern edge's northing on the central meridian; the square's southern edge is the first
    # row * 100 km + k * 2000 km at most 100 km below it
    _, nb = merc_to_utm(np.array([math.radians(zone * 6 - 183) * R]),
                        np.array([R * math.log(math.tan(math.pi / 4 + math.radians(lat0) / 2))]), zone, south)
    nb = float(nb[0])
    n0 = row * 100000
    while n0 < nb - 100000.0:
        n0 += 2000000
    fn = 10000000 if south else 0
    return float(e0 // 60 * 60), float(fn + -((fn - (n0 + 100000)) // 60) * 60)


# ---------------------------------------------------------------------------------------------------------------------
# packs


def pack_grid(z, px, py, margin=0):
    """EPSG:3857 x (cols) and y (rows) of the pixel centres of pack (z, px, py), PACK_PX + 2 margin each."""
    n = 1 << (z - K)
    res = SPAN / (n * PACK_PX)
    i = np.arange(-margin, PACK_PX + margin, dtype=np.float64) + 0.5
    x = (px * PACK_PX + i) * res - SPAN / 2
    y = SPAN / 2 - (py * PACK_PX + i) * res
    return x, y


def pack_ll(z, px, py):
    """(lat_s, lon_w, lat_n, lon_e) of pack (z, px, py)."""
    n = 1 << (z - K)

    def lat(t):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / n))))
    return lat(py + 1), px / n * 360 - 180, lat(py), (px + 1) / n * 360 - 180


def _nodes_utm(x, y, zone, south):
    """UTM easting / northing (float64, mm-rounded) of every pixel centre, from the node grid every NODE_PX pixels
    (bilinear between nodes: < 1 mm off the true mapping over 16 pixels)."""
    h, w = y.size, x.size
    ni = np.arange(0, w + NODE_PX, NODE_PX)
    nj = np.arange(0, h + NODE_PX, NODE_PX)
    step = x[1] - x[0]
    nx = x[0] + ni * step
    ny = y[0] - nj * step
    E, N = merc_to_utm(np.broadcast_to(nx[None, :], (nj.size, ni.size)),
                       np.broadcast_to(ny[:, None], (nj.size, ni.size)),
                       zone, south)
    E = np.floor(E * 1000.0 + 0.5).astype(np.int64)      # mm
    N = np.floor(N * 1000.0 + 0.5).astype(np.int64)
    # integer bilinear between nodes (exact)
    jj, ii = np.arange(h), np.arange(w)
    j0, fj = jj // NODE_PX, jj % NODE_PX
    i0, fi = ii // NODE_PX, ii % NODE_PX

    def interp(V):
        a = V[j0][:, i0] * (NODE_PX - fi)[None, :] + V[j0][:, i0 + 1] * fi[None, :]
        b = V[j0 + 1][:, i0] * (NODE_PX - fi)[None, :] + V[j0 + 1][:, i0 + 1] * fi[None, :]
        return a * (NODE_PX - fj)[:, None] + b * fj[:, None]          # mm * NODE_PX^2
    return interp(E), interp(N)                                       # (int64, mm * 256)


class TileGeo:
    """A tile's pixels in a pack: their taps in its 10 m grid (fixed point, QB bits) and 20 m SCL cells, the windows
    to read; shared by every scene of the tile (one grid per tile)."""

    def __init__(self, rows_u, cols_u, inside, origin):
        q = 1 << QB
        cu, ru = cols_u[inside], rows_u[inside]
        c = cu - q // 2
        r = ru - q // 2
        c0, r0 = c >> QB, r >> QB
        fc, fr = c & (q - 1), r & (q - 1)
        self.n = cu.size
        self.origin = origin
        if not self.n:
            return
        self.R0, self.R1 = int(r0.min()), int(r0.max()) + 2
        self.C0, self.C1 = int(c0.min()), int(c0.max()) + 2
        self.w = tuple(w.astype(np.int32) for w in ((q - fc) * (q - fr), fc * (q - fr), (q - fc) * fr, fc * fr))
        self.rr, self.cc = (r0 - self.R0).astype(np.intp), (c0 - self.C0).astype(np.intp)
        su, sv = cu >> (QB + 1), ru >> (QB + 1)
        self.S0, self.S1 = int(sv.min()) - GROW_PX, int(sv.max()) + GROW_PX + 1
        self.T0, self.T1 = int(su.min()) - GROW_PX, int(su.max()) + GROW_PX + 1
        self.sr, self.sc = sv - self.S0, su - self.T0


BANDS = ("B04", "B03", "B02")


def start_scene(item_id, geo):
    """Open the scene's COGs and submit its reads (None: nothing to read / another grid than the tile's)."""
    if not geo.n:
        return None
    cs = [cog(item_id, a) for a in BANDS + ("SCL",)]
    if geo.origin is not None and any(c.origin != geo.origin for c in cs):
        return None                     # (another grid than the tile's: never seen; left out rather than misplaced)
    hs = [c.start(geo.R0, geo.R1, geo.C0, geo.C1) for c in cs[:3]]
    hs.append(cs[3].start(geo.S0, geo.S1, geo.T0, geo.T1))
    return cs, hs


def finish_scene(started, geo):
    """(refl (n, 3) uint16 reflectance x 10^4, flags (n,) uint8: 0 clear, 1 snow, 2 masked, 3 no data)."""
    n = geo.n
    flags = np.full(n, 3, np.uint8)
    refl = np.zeros((n, 3), np.uint16)
    if started is None:
        return refl, flags
    cs, hs = started
    q = 1 << QB
    ok = np.ones(n, bool)
    rr, cc = geo.rr, geo.cc
    w00, w01, w10, w11 = geo.w
    for bi in range(3):
        win = cs[bi].finish(hs[bi]).astype(np.int32)       # (65535 x 4096 < 2^31: the sum fits)
        a00, a01, a10, a11 = win[rr, cc], win[rr, cc + 1], win[rr + 1, cc], win[rr + 1, cc + 1]
        ok &= (a00 > 0) & (a01 > 0) & (a10 > 0) & (a11 > 0)
        v = (a00 * w00 + a01 * w01 + a10 * w10 + a11 * w11 + (q * q) // 2) >> (2 * QB)
        refl[:, bi] = np.clip(v - OFFSET_DN, 0, 65000).astype(np.uint16)
    # SCL (20 m), nearest; the masked classes grown GROW_PX on its own grid (read with that margin)
    scl = cs[3].finish(hs[3])
    bad = grow(np.isin(scl, BAD), GROW_PX)
    s = scl[geo.sr, geo.sc]
    flags[:] = 0
    flags[s == SNOW] = 1
    flags[bad[geo.sr, geo.sc]] = 2
    flags[(s == 0) | ~ok] = 3
    refl[flags == 3] = 0
    return refl, flags


def read_scene(item_id, rows_u, cols_u, inside, origin=None):
    geo = TileGeo(rows_u, cols_u, inside, origin)
    return finish_scene(start_scene(item_id, geo), geo)


def grow(mask, r):
    """Binary dilation by a disc of radius r (integer offsets: dx^2 + dy^2 <= r^2), via a summed-area per row offset."""
    h, w = mask.shape
    out = np.zeros_like(mask)
    pad = np.zeros((h + 2 * r, w + 2 * r), bool)
    pad[r:r + h, r:r + w] = mask
    for dy in range(-r, r + 1):
        dx = int(math.isqrt(r * r - dy * dy))
        rowsrc = pad[r + dy:r + dy + h]
        # any over [x - dx, x + dx]: a running count
        cs = np.zeros((h, w + 2 * r + 1), np.int32)
        np.cumsum(rowsrc, axis=1, out=cs[:, 1:])
        lo = np.arange(w) + r - dx
        hi = np.arange(w) + r + dx + 1
        out |= (cs[:, hi] - cs[:, lo]) > 0
    return out


def composite(refl, flags, method=None):
    """Per pixel over looks: refl (L, n, 3) uint16, flags (L, n) -> (n, 3) uint16 value and (n,) bool valid. The looks
    used: the clear ones (>= MIN_CLEAR, or one with no snow look), else the clear and snow ones, else the darker half of
    every look with data. Of those, per band: "qNN" the NN % quantile (the nearest rank), "median" the median (an even
    count: the rounded mean of the middle two); "medoid" / "bqNN" one whole look (compared, not used). Integer."""
    method = method or COMPOSITE
    L, n = flags.shape
    clear = flags == 0
    snow = flags == 1
    data = flags < 3
    nc = clear.sum(0)
    ns = snow.sum(0)
    nd = data.sum(0)
    use = np.where((nc >= MIN_CLEAR)[None, :] | ((nc >= 1) & (ns == 0))[None, :], clear,
                   np.where(((nc + ns) >= 1)[None, :], clear | snow, data))
    out = np.zeros((n, 3), np.uint16)
    # the darker half where nothing is clear: by brightness among the looks with data
    fb = np.flatnonzero((nc + ns) == 0)
    if fb.size:                     # (only those pixels: the ranks of every pixel's looks were GBs)
        r = refl[:, fb]
        lum = r[..., 0].astype(np.int64) * 2 + r[..., 1].astype(np.int64) * 5 + r[..., 2].astype(np.int64)
        lum = np.where(data[:, fb], lum, np.iinfo(np.int64).max)
        rank = np.argsort(np.argsort(lum, axis=0, kind="stable"), axis=0, kind="stable")
        half = np.maximum(1, (nd[fb] + 1) // 2)
        use[:, fb] = data[:, fb] & (rank < half[None, :])
    k = use.sum(0)
    valid = k > 0
    if method.startswith("q"):
        # per band, the look at quantile p of the looks used (q40: 40 %: haze and cloud fringes are brighter)
        p = int(method[1:]) / 100.0
        for b in range(3):
            v = np.sort(np.where(use, refl[..., b], np.uint16(65535)), axis=0)
            idx = ((k - 1).clip(0) * int(p * 100) + 50) // 100
            out[:, b] = np.take_along_axis(v, idx[None, :], 0)[0]
    elif method == "median":
        for b in range(3):
            v = np.where(use, refl[..., b], np.uint16(65535))
            v = np.sort(v, axis=0)
            lo = np.maximum(0, (k - 1) // 2)
            hi = np.maximum(0, k // 2)
            a = np.take_along_axis(v, lo[None, :], 0)[0].astype(np.int64)
            c = np.take_along_axis(v, hi[None, :], 0)[0].astype(np.int64)
            out[:, b] = ((a + c + 1) // 2).astype(np.uint16)
    elif method == "medoid":
        # the look closest (sum of L1 distances over bands) to the other looks used
        x = refl.astype(np.int64)
        best = np.full(n, np.iinfo(np.int64).max)
        pick = np.zeros(n, np.int64)
        for i in range(L):
            d = np.zeros(n, np.int64)
            for j in range(L):
                if i != j:
                    d += np.where(use[j], np.abs(x[i] - x[j]).sum(-1), 0)
            d = np.where(use[i], d, np.iinfo(np.int64).max)
            better = d < best
            best = np.where(better, d, best)
            pick = np.where(better, i, pick)
        out = np.take_along_axis(refl, pick[None, :, None], 0)[0]
    elif method.startswith("bq"):
        # the look at brightness quantile p (bq40: 40 %) among the looks used
        p = int(method[2:]) / 100.0
        lum = refl[..., 0].astype(np.int64) * 2 + refl[..., 1].astype(np.int64) * 5 + refl[..., 2].astype(np.int64)
        lum = np.where(use, lum, np.iinfo(np.int64).max)
        order = np.argsort(lum, axis=0, kind="stable")
        idx = np.floor((k - 1).clip(0) * p + 0.5).astype(np.int64)
        pick = np.take_along_axis(order, idx[None, :], 0)[0]
        out = np.take_along_axis(refl, pick[None, :, None], 0)[0]
    else:
        raise ValueError(method)
    out[~valid] = 0
    return out, valid


def pack_stack(z, px, py, margin, tiles, workers=8, log=None):
    """Per MGRS tile over pack (z, px, py) (+ margin pixels): see grid_stack."""
    x, y = pack_grid(z, px, py, margin)
    return grid_stack(x, y, tiles, log)


def grid_stack(x, y, tiles, log=None):
    """Per MGRS tile over the grid of pixel centres x (cols) / y (rows) (EPSG:3857, square pixels): (code, weight
    (h, w) int64, looks: [(refl, flags)], inside (h, w) bool, ids) for each tile whose square holds a pixel.
    tiles: {code: tile_scenes(...)}."""
    h, w = y.size, x.size
    pending = []
    for code in sorted(tiles):
        t = tiles[code]
        if not t["scenes"]:
            continue
        m = CODE_RE.fullmatch(code)
        zone, south = int(m.group(1)), m.group(2) < "N"
        E, N = _nodes_utm(x, y, zone, south)           # mm * NODE_PX^2
        sc = NODE_PX * NODE_PX * 1000
        ids = [s["id"] for s in t["scenes"]]
        org = cog(ids[0], "B04").origin              # (the tile's corner as its COGs give it: 60 m grid)
        if org is None:
            continue
        ulx, uly = int(round(org[0] * 1000)) * NODE_PX * NODE_PX, int(round(org[1] * 1000)) * NODE_PX * NODE_PX
        du = E - ulx                                    # metres east of the tile's west edge, in sc units
        dv = uly - N
        side = 109800 * sc
        inside = (du >= 0) & (du < side) & (dv >= 0) & (dv < side)
        if not inside.any():
            continue
        # fixed point position in the 10 m grid: du / (10 m) * 2^QB, rounded
        unit = 10 * sc
        cols_u = (du * (1 << QB) + unit // 2) // unit
        rows_u = (dv * (1 << QB) + unit // 2) // unit
        # keep a pixel only where all four bilinear taps fall in the tile (the half pixel round its edge: the neighbour)
        q = 1 << QB
        inside &= (cols_u >= q // 2) & (rows_u >= q // 2) & (cols_u < (10980 - 1) * q + q // 2) & \
            (rows_u < (10980 - 1) * q + q // 2)
        if not inside.any():
            continue
        # the blend weight: metres into the square from its nearest edge, / FEATHER_M, 0..1024
        dmin = np.minimum(np.minimum(du, side - du), np.minimum(dv, side - dv))
        wgt = np.clip((dmin * 1024) // int(FEATHER_M * sc), 0, 1024)
        wgt = np.maximum(wgt, 1) * inside
        geo = TileGeo(rows_u, cols_u, inside, org)
        with ThreadPoolExecutor(min(HEAD_THREADS, len(ids))) as ex:     # (headers in parallel; reads submitted)
            started = list(ex.map(lambda i: start_scene(i, geo), ids))
        pending.append((code, wgt, inside, ids, geo, started))
    # (every tile's reads in flight before the first is decoded)
    out = []
    for code, wgt, inside, ids, geo, started in pending:
        # (one stack per tile, each scene decoded straight into its row, its fetched bytes dropped once decoded)
        refl = np.empty((len(ids), geo.n, 3), np.uint16)
        flags = np.empty((len(ids), geo.n), np.uint8)

        def one(i):
            refl[i], flags[i] = finish_scene(started[i], geo)
            started[i] = None
        with ThreadPoolExecutor(min(SCENE_THREADS, len(ids))) as ex:
            list(ex.map(one, range(len(ids))))
        out.append((code, wgt, (refl, flags), inside, ids))
        if log:
            log(f"  {code}: {len(ids)} scenes, {int(inside.sum())} px")
    return out, (h, w)


def blend(stack, shape, method=None):
    """The tiles' composites blended by their weights: (h, w, 3) uint16 reflectance and (h, w) valid."""
    h, w = shape
    acc = np.zeros((h * w, 3), np.int64)
    wsum = np.zeros(h * w, np.int64)
    for code, wgt, looks, inside, ids in stack:
        sel = inside.ravel()
        if isinstance(looks, tuple):            # (grid_stack's: the stacked arrays)
            refl, flags = looks
        else:
            refl = np.stack([lk[0] for lk in looks])
            flags = np.stack([lk[1] for lk in looks])
        n = flags.shape[1]
        v = np.empty((n, 3), np.uint16)
        ok = np.empty(n, bool)
        for a in range(0, n, COMPOSITE_CHUNK):  # (pixel-local: chunks bound the looks x pixels temporaries)
            v[a:a + COMPOSITE_CHUNK], ok[a:a + COMPOSITE_CHUNK] = composite(refl[:, a:a + COMPOSITE_CHUNK],
                                                                            flags[:, a:a + COMPOSITE_CHUNK], method)
        wt = wgt.ravel()[sel] * ok
        idx = np.flatnonzero(sel)
        acc[idx] += v.astype(np.int64) * wt[:, None]
        wsum[idx] += wt
    valid = wsum > 0
    out = np.zeros((h * w, 3), np.uint16)
    out[valid] = ((acc[valid] + wsum[valid, None] // 2) // wsum[valid, None]).astype(np.uint16)
    return out.reshape(h, w, 3), valid.reshape(h, w)


_LUT = {}


def tone_lut(tone=None):
    tone = tone or TONE
    key = tuple(sorted(tone.items()))
    if key not in _LUT:
        r = np.arange(65536, dtype=np.float64)
        x = np.clip((r - tone["black"]) / (tone["white"] - tone["black"]), 0.0, 1.0)
        # 12-bit output, x ^ (1 / gamma) by this file's exp / log (the same table on every machine; its SHA-256 is
        # in the record)
        p = np.where(x > 0, _exp(_log(np.where(x > 0, x, 1.0)) / tone["gamma"]), 0.0)
        lut = np.floor(p * 4095.0 + 0.5).astype(np.int64)
        _LUT[key] = lut
    return _LUT[key]


def lut_sha(tone=None):
    return hashlib.sha256(tone_lut(tone).astype("<i8").tobytes()).hexdigest()[:16]


def _box(a, r):
    """Mean over the (2r + 1)^2 box round each pixel (edges: the edge values repeated), integer (floor)."""
    p = np.pad(a, r, mode="edge").astype(np.int64)
    c = np.cumsum(p, axis=0)
    c = np.concatenate([np.zeros((1, c.shape[1]), np.int64), c], axis=0)
    v = c[2 * r + 1:] - c[:-2 * r - 1]
    c = np.cumsum(v, axis=1)
    c = np.concatenate([np.zeros((c.shape[0], 1), np.int64), c], axis=1)
    return (c[:, 2 * r + 1:] - c[:, :-2 * r - 1]) // ((2 * r + 1) ** 2)


def _all_valid(valid, r):
    """Pixels whose (2r + 1)^2 box is all data."""
    return (_box(valid.astype(np.int64), r) == 1) & valid if r else valid


def tone_map(refl, valid, tone=None, sharpen=None, clarity=None):
    """uint16 reflectance (h, w, 3) -> uint8 sRGB (h, w, 3), black where not valid. Integer. sharpen / clarity
    (/ 16): unsharp masks of the luminance (the same change on the three channels: no colour fringes) with a 3 x 3
    binomial and a CLARITY_R box blur, where the box is all data (none across a coast's black)."""
    tone = tone or TONE
    sharpen = SHARPEN if sharpen is None else sharpen
    clarity = CLARITY if clarity is None else clarity
    x = tone_lut(tone)[refl]                                     # 0..4095
    lum = (x[..., 0] * 54 + x[..., 1] * 183 + x[..., 2] * 19) >> 8
    sat = int(round(tone["sat"] * 256))
    x = lum[..., None] + (((x - lum[..., None]) * sat) >> 8)
    x = np.clip(x, 0, 4095)
    if sharpen or clarity:
        lum = (x[..., 0] * 54 + x[..., 1] * 183 + x[..., 2] * 19) >> 8
        d = np.zeros_like(lum)
        if sharpen:
            p = np.pad(lum, 1, mode="edge")
            bl = p[:-2] + 2 * p[1:-1] + p[2:]
            bl = (bl[:, :-2] + 2 * bl[:, 1:-1] + bl[:, 2:] + 8) >> 4
            d += np.where(_all_valid(valid, 1), ((lum - bl) * sharpen) >> 4, 0)
        if clarity:
            d += np.where(_all_valid(valid, CLARITY_R), ((lum - _box(lum, CLARITY_R)) * clarity) >> 4, 0)
        x = np.clip(x + d[..., None], 0, 4095)
    img = ((x * 255 + 2047) // 4095).astype(np.uint8)
    mx = img.max(-1)
    lift = valid & (mx < DATA_MIN)
    if lift.any():
        img[lift] = np.maximum(img[lift], DATA_MIN)
    img[~valid] = 0
    return img


def encode_tiles(img, valid):
    """{index: WebP bytes} of the pack's 8 x 8 tiles (a tile with no data left out)."""
    tiles = {}
    for j in range(1 << K):
        for i in range(1 << K):
            sl = (slice(j * TILE, (j + 1) * TILE), slice(i * TILE, (i + 1) * TILE))
            if not valid[sl].any():
                continue
            b = io.BytesIO()
            Image.fromarray(np.ascontiguousarray(img[sl])).save(b, "WEBP", quality=WEBP_Q, method=WEBP_METHOD)
            tiles[j * (1 << K) + i] = b.getvalue()
    return tiles


def down2(img, valid):
    """The 2 x 2 mean (rounded) of img (h, w, 3) uint8; black (not valid) where any of the four is not (bngs2's
    rule)."""
    h, w = valid.shape
    v = img.reshape(h // 2, 2, w // 2, 2, 3).astype(np.uint16)
    out = ((v.sum(axis=(1, 3)) + 2) >> 2).astype(np.uint8)
    ok = valid.reshape(h // 2, 2, w // 2, 2).all(axis=(1, 3))
    out[~ok] = 0
    return out, ok


SHARP_MARGIN = 5            # (the pack's pixels computed round it: CLARITY_R + 1)


def photo_pack(z, px, py, tiles, workers=8, method=None, tone=None, sharpen=None, log=None):
    """Pack (z, px, py) from the scenes: (img (2048, 2048, 3) uint8, valid) or (None, None) when no tile holds it."""
    m = SHARP_MARGIN
    stack, shape = pack_stack(z, px, py, m, tiles, workers, log)
    if not stack:
        return None, None
    refl, valid = blend(stack, shape, method)
    img = tone_map(refl, valid, tone, sharpen)
    return img[m:-m, m:-m], valid[m:-m, m:-m]


def pack_bytes(z, px, py, img, valid):
    if img is None or not valid.any():
        return None
    tiles = encode_tiles(img, valid)
    return tilepack.build(z, px, py, K, tiles) if tiles else None


# ---------------------------------------------------------------------------------------------------------------------
# shards


def shard_ll(x8, y8):
    n = 1 << SHARD_Z

    def lat(t):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / n))))
    return lat(y8 + 1), x8 / n * 360 - 180, lat(y8), (x8 + 1) / n * 360 - 180


def photo14(z, px, py, max_lat):
    """Is zoom-14 pack (px, py) a photo pack (it reaches within max_lat degrees of the equator)?"""
    s, _, n, _ = pack_ll(z, px, py)
    return s < max_lat and n > -max_lat


def shard_plan(x8, y8, max_lat):
    """The shard's packs: [(zoom, px, py, "photo" | "down")] coarse-last, finest first."""
    d14 = 14 - K - SHARD_Z                  # 3: 8 x 8 zoom-14 packs
    p14 = [(14, (x8 << d14) + i, (y8 << d14) + j) for j in range(1 << d14) for i in range(1 << d14)]
    photo = {p for p in p14 if photo14(*p, max_lat)}
    plan = [(14, px, py, "photo") for (_, px, py) in p14 if (14, px, py) in photo]
    for j in range(4):
        for i in range(4):
            px, py = (x8 << 2) + i, (y8 << 2) + j
            kids = [(14, 2 * px + a, 2 * py + b) for b in (0, 1) for a in (0, 1)]
            plan.append((13, px, py, "down" if all(k in photo for k in kids) else "photo"))
    for j in range(2):
        for i in range(2):
            plan.append((12, (x8 << 1) + i, (y8 << 1) + j, "down"))
    plan.append((11, x8, y8, "down"))
    return plan


def _photo_job(args):
    """A worker's photo pack: ((z, px, py), pack bytes or None, (img, valid) or None, stats)."""
    global FETCH_THREADS
    z, px, py, tiles, threads = args
    if _POOL is None:
        FETCH_THREADS = threads
    t0 = time.time()
    b0, q0 = STATS["bytes"], STATS["requests"]
    img, valid = photo_pack(z, px, py, tiles, threads)
    data = pack_bytes(z, px, py, img, valid)
    keep = None if img is None or not valid.any() else (img, valid)
    return (z, px, py), data, keep, {"s": round(time.time() - t0, 1), "read": STATS["bytes"] - b0,
                                     "requests": STATS["requests"] - q0, "peak_mb": peak_mb()}


def _worker_init():
    try:
        os.nice(5)
    except (OSError, AttributeError):     # (Windows: no os.nice; start the build with low priority there)
        pass


def tiles_for_shard(x8, y8, cache):
    """{code: tile_scenes(code)} of the MGRS tiles over shard (x8, y8)."""
    codes = tiles_over(shard_ll(x8, y8), cache)
    with ThreadPoolExecutor(4) as ex:
        return dict(zip(codes, ex.map(lambda c: tile_scenes(c, cache), codes)))


def builder_id():
    """The SHA-256 of this file (the record's "builder": the upload checker requires its own copy's; tilepack.py's
    packs are the same bytes in either repository's copy)."""
    with open(os.path.abspath(__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


WORLDCOVER_LIST = ("https://esa-worldcover-s2.s3.eu-central-1.amazonaws.com/"
                   "?list-type=2&prefix=rgbnir/2021/&max-keys=1000")


def planet_shards():
    """The zoom-8 shards of the planet: every zoom-8 tile meeting a 1-degree cell of ESA WorldCover's 2021 Sentinel-2
    composite (the land and its coasts: the 2021 packs' own coverage, tiles.yml), sorted. A listing of a public
    bucket; the workflow keeps the result (tools/s2c_shards.txt) so every run plans the same shards."""
    names, token = [], None
    while True:
        u = WORLDCOVER_LIST + ("&continuation-token=" + urllib.parse.quote(token) if token else "")
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=60) as r:
            body = r.read().decode()
        names += re.findall(r"_([NS]\d\d)([EW]\d{3})_S2RGBNIR\.tif", body)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", body)
        if not m:
            break
        token = m.group(1)
    out = set()
    for ns, ew in set(names):
        la = int(ns[1:]) * (1 if ns[0] == "N" else -1)
        lo = int(ew[1:]) * (1 if ew[0] == "E" else -1)
        out.update(shards_in(la + 1e-9, lo + 1e-9, la + 1 - 1e-9, lo + 1 - 1e-9))
    return sorted(out, key=lambda t: (t[1], t[0]))


def params():
    """Everything that changes the output beyond the scenes (the record's "params")."""
    return {"format": FORMAT, "prefix": PREFIX, "collection": COLLECTION, "target_clear": TARGET_CLEAR,
            "min_per_orbit": MIN_PER_ORBIT, "max_per_orbit": MAX_PER_ORBIT, "min_item_clear": MIN_ITEM_CLEAR,
            "min_item_data": MIN_ITEM_DATA, "max_cloud": MAX_CLOUD, "bad": list(BAD), "grow_px": GROW_PX,
            "min_clear": MIN_CLEAR, "feather_m": FEATHER_M, "offset_dn": OFFSET_DN, "data_min": DATA_MIN,
            "composite": COMPOSITE, "tone": TONE, "lut": lut_sha(), "sharpen": SHARPEN, "clarity": [CLARITY, CLARITY_R],
            "webp": [WEBP_Q, WEBP_METHOD],
            "pillow": PIL.__version__, "numpy": np.__version__}


def record_rel(x8, y8):
    return f"_build/{PREFIX}/shards/{x8}_{y8}.json"


def build_shard(out, x8, y8, workers=3, threads=FETCH_THREADS, cache=None, max_lat=MAX_LAT_14, log=print, pool=None):
    """Every pack of zoom-8 shard (x8, y8) into out, then its record (last). Returns the record."""
    t0 = time.time()
    tiles = tiles_for_shard(x8, y8, cache)
    plan = shard_plan(x8, y8, max_lat)
    S13 = 4 * PACK_PX                       # the shard's zoom-13 mosaic: 4 x 4 packs of 2048 px
    mos = np.zeros((S13, S13, 3), np.uint8)
    mval = np.zeros((S13, S13), bool)
    files, stats = [], []
    jobs = [(z, px, py, tiles, threads) for (z, px, py, how) in plan if how == "photo"]
    results = pool.map(_photo_job, jobs) if pool is not None else map(_photo_job, jobs)
    read = reqs = 0
    z13 = []
    for (z, px, py), data, keep, st in results:
        read += st["read"]
        reqs += st["requests"]
        stats.append({"pack": f"{z}/{px}/{py}", **st})
        _write(out, z, px, py, data, files)
        if keep is None:
            continue
        if z == 14:
            i, j = px - (x8 << 3), py - (y8 << 3)
            h = PACK_PX // 2
            mos[j * h:(j + 1) * h, i * h:(i + 1) * h], mval[j * h:(j + 1) * h, i * h:(i + 1) * h] = down2(*keep)
        else:
            z13.append(((px, py), keep))
    for (px, py), (img, valid) in z13:       # (after the zoom-14 means: a zoom-13 photo pack is the zoom's own)
        i, j = px - (x8 << 2), py - (y8 << 2)
        mos[j * PACK_PX:(j + 1) * PACK_PX, i * PACK_PX:(i + 1) * PACK_PX] = img
        mval[j * PACK_PX:(j + 1) * PACK_PX, i * PACK_PX:(i + 1) * PACK_PX] = valid
    for (z, px, py, how) in plan:
        if z == 13 and how == "down":
            i, j = px - (x8 << 2), py - (y8 << 2)
            sl = (slice(j * PACK_PX, (j + 1) * PACK_PX), slice(i * PACK_PX, (i + 1) * PACK_PX))
            _write(out, 13, px, py, pack_bytes(13, px, py, mos[sl], mval[sl]), files)
    m12, v12 = down2(mos, mval)
    for j in range(2):
        for i in range(2):
            sl = (slice(j * PACK_PX, (j + 1) * PACK_PX), slice(i * PACK_PX, (i + 1) * PACK_PX))
            px, py = (x8 << 1) + i, (y8 << 1) + j
            _write(out, 12, px, py, pack_bytes(12, px, py, m12[sl], v12[sl]), files)
    m11, v11 = down2(m12, v12)
    _write(out, 11, x8, y8, pack_bytes(11, x8, y8, m11, v11), files)
    rec = {"format": FORMAT, "shard": [x8, y8], "builder": builder_id(), "params": params(),
           "packs": sorted(files, key=lambda f: f["path"]),
           "tiles": {c: {"stages": t["stages"], "scenes": [s["id"] for s in t["scenes"]]}
                     for c, t in sorted(tiles.items())},
           "read_bytes": read, "requests": reqs, "seconds": round(time.time() - t0, 1), "pack_stats": stats}
    rp = os.path.join(out, record_rel(x8, y8))
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    with open(rp + ".tmp", "w") as fh:
        json.dump(rec, fh, sort_keys=True, separators=(",", ":"))
    os.replace(rp + ".tmp", rp)
    log(json.dumps({"shard": f"{x8},{y8}", "packs": len(files), "mb": round(sum(f["bytes"] for f in files) / 1e6, 2),
                    "read_mb": round(read / 1e6, 1), "seconds": rec["seconds"],
                    "scenes": sum(len(t["scenes"]) for t in tiles.values()), "tiles": len(tiles)}))
    return rec


def _write(out, z, px, py, data, files):
    if data is None:
        return
    rel = tilepack.path(z, px, py)
    p = os.path.join(out, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(p + ".tmp", p)
    files.append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})


def _src_pack(src, z, px, py):
    """A pack's bytes from a folder or a base URL (the tile host: the published packs), or None."""
    rel = tilepack.path(z, px, py)
    if src.startswith(("https://", "http://")):
        u = src.rstrip("/") + "/" + rel
        for attempt in range(5):
            try:
                with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=60) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if attempt == 4:
                    raise
            except OSError:
                if attempt == 4:
                    raise
            time.sleep(2.0 * 2 ** attempt)
    p = os.path.join(src, rel)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as fh:
        return fh.read()


def _decode_pack(data):
    """A pack's (2048, 2048, 3) uint8 image and valid mask (black / missing tiles: not valid)."""
    img = np.zeros((PACK_PX, PACK_PX, 3), np.uint8)
    val = np.zeros((PACK_PX, PACK_PX), bool)
    for k, b in tilepack.parse(data)[4].items():
        tj, ti = divmod(k, 1 << K)
        t = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"))
        img[tj * TILE:(tj + 1) * TILE, ti * TILE:(ti + 1) * TILE] = t
        val[tj * TILE:(tj + 1) * TILE, ti * TILE:(ti + 1) * TILE] = t.max(-1) > imagery_nodata_max()
    return img, val


def build_coarse(out, src, x5, y5, log=print):
    """Zooms 10..8 of zoom-5 region (x5, y5) from its zoom-11 packs (see build_down)."""
    return build_down(out, src, "coarse", 11, 8, x5, y5, log)


def build_world(out, src, log=print):
    """Zooms 7..3 of the world from the zoom-8 packs (see build_down): the 2021 packs' coarse zooms' successor, once
    every region's coarse zooms are published."""
    return build_down(out, src, "world", 8, 3, 0, 0, log)


def build_down(out, src, kind, base_z, top_z, x, y, log=print):
    """The packs of zooms base_z - 1 .. top_z under pack (top_z, x, y) from the base_z packs in src (a folder or the
    tile host's base URL; decoded, 2 x 2 means, black where any of the four is, as bngs2's coarse zooms), written to
    out, then the record _build/s2c/v1/<kind>/<x>_<y>.json; returns the record. Each pack from the 2 x 2 packs of the
    zoom below (a few hundred MB at most in memory)."""
    t0 = time.time()
    files = []
    have = [0]

    def level(z, px, py):
        """(img, valid) of pack (z, px, py), written when z < base_z, or None (no data under it)."""
        if z == base_z:
            data = _src_pack(src, z, px, py)
            if data is None:
                return None
            have[0] += 1
            return _decode_pack(data)
        kids = [[level(z + 1, 2 * px + i, 2 * py + j) for i in (0, 1)] for j in (0, 1)]
        if all(k is None for row in kids for k in row):
            return None
        img = np.zeros((2 * PACK_PX, 2 * PACK_PX, 3), np.uint8)
        val = np.zeros((2 * PACK_PX, 2 * PACK_PX), bool)
        for j in (0, 1):
            for i in (0, 1):
                if kids[j][i] is not None:
                    sl = (slice(j * PACK_PX, (j + 1) * PACK_PX), slice(i * PACK_PX, (i + 1) * PACK_PX))
                    img[sl], val[sl] = kids[j][i]
        got = down2(img, val)
        _write(out, z, px, py, pack_bytes(z, px, py, *got), files)
        return got
    level(top_z, x, y)
    rec = {"format": FORMAT, kind: [x, y], "builder": builder_id(), "params": params(), "from_packs": have[0],
           "packs": sorted(files, key=lambda f: f["path"]), "seconds": round(time.time() - t0, 1)}
    rp = os.path.join(out, f"_build/{PREFIX}/{kind}/{x}_{y}.json")
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    with open(rp + ".tmp", "w") as fh:
        json.dump(rec, fh, sort_keys=True, separators=(",", ":"))
    os.replace(rp + ".tmp", rp)
    log(json.dumps({kind: f"{x},{y}", "from": have[0], "packs": len(files)}))
    return rec


def selftest():
    """SHA-256 of a pack built from synthetic looks through the whole pixel pipeline (positions, composite, blend,
    tone, sharpening, WebP): the same on every machine whose Pillow is the pinned one (the workflow's; compare a home
    machine's with a runner's before it builds shards)."""
    x, y = pack_grid(14, 8662, 5789, SHARP_MARGIN)
    h = y.size
    rng = np.random.default_rng(20251123)              # (PCG64: the same stream on every platform)
    L, n = 9, h * h
    base = rng.integers(200, 4000, size=(n, 3), dtype=np.int64)
    refl = np.clip(base[None] + rng.integers(-300, 300, size=(L, n, 3)), 0, 65000).astype(np.uint16)
    flags = rng.choice(np.array([0, 0, 0, 0, 1, 2, 3], np.uint8), size=(L, n))
    E, N = _nodes_utm(x, y, 32, False)
    wgt = np.clip(((E - E.min()) * 1024) // max(1, int(E.max() - E.min())), 1, 1024)
    inside = np.ones((h, h), bool)
    looks = [(refl[i], flags[i]) for i in range(L)]
    v, valid = blend([("SELF", wgt, looks, inside, [])], (h, h))
    img = tone_map(v, valid)[SHARP_MARGIN:-SHARP_MARGIN, SHARP_MARGIN:-SHARP_MARGIN]
    data = pack_bytes(14, 8662, 5789, img, valid[SHARP_MARGIN:-SHARP_MARGIN, SHARP_MARGIN:-SHARP_MARGIN])
    return hashlib.sha256(data).hexdigest() + f"  (pillow {PIL.__version__}, numpy {np.__version__}, lut {lut_sha()})"


def peak_mb():
    """This process's peak resident memory (MB), or None (no resource module: Windows)."""
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    except (ImportError, OSError):
        return None


def imagery_nodata_max():
    return 6                                # (server/imagery.py NODATA_MAX: a pixel this dark is "no data")


def shards_in(s, w, n, e):
    m = 1 << SHARD_Z

    def xy(lat, lon):
        x = (lon + 180.0) / 360.0 * m
        y = (1 - math.asinh(math.tan(math.radians(max(-85.05, min(85.05, lat))))) / math.pi) / 2 * m
        return int(min(m - 1, max(0, x))), int(min(m - 1, max(0, y)))
    x0, y0 = xy(n, w)
    x1, y1 = xy(s, e)
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def parse_xy(text, lim):
    out = []
    for tok in text.replace(";", " ").split():
        m = re.fullmatch(r"(\d+),(\d+)", tok)
        if not m or int(m.group(1)) >= lim or int(m.group(2)) >= lim:
            raise SystemExit(f"bad tile {tok!r} (x,y below {lim})")
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--shards", default="", help='zoom-8 tiles "x,y x,y ..."')
    ap.add_argument("--shard-file", help='a file of zoom-8 tiles ("x,y", one a line or space separated)')
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("S", "W", "N", "E"), help="the shards over a box")
    ap.add_argument("--coarse", default="", help='zoom-5 regions "x,y ...": zooms 10..8 from --from\'s zoom-11 packs')
    ap.add_argument("--world", action="store_true", help="zooms 7..3 from --from's zoom-8 packs")
    ap.add_argument("--from", dest="src", help="--coarse / --world: the folder or base URL "
                                               "(https://tiles.tgforever.net/s2c/v1) of the packs ({z}/{px}/{py}.bngp)")
    ap.add_argument("--pack", type=int, nargs=3, metavar=("Z", "PX", "PY"), help="one photo pack (no record)")
    ap.add_argument("--max-lat", type=float, default=MAX_LAT_14, help="zoom 14 within this latitude, 13 beyond")
    ap.add_argument("--skip", help="a file of keys or paths the bucket has (a shard whose record is listed is skipped)")
    ap.add_argument("--deadline", type=float, help="a Unix time: no shard started after it (exit 75)")
    ap.add_argument("--workers", type=int, default=3, help="pack processes")
    ap.add_argument("--threads", type=int, default=FETCH_THREADS, help="fetch threads per pack process")
    ap.add_argument("--cache", help="a folder for the STAC answers (tile lists, scene choices); default OUT/_stac")
    ap.add_argument("--list-planet", action="store_true", help="write the planet's shards to OUT (a file) and stop")
    ap.add_argument("--stac-only", action="store_true",
                    help="only the shards' STAC answers into --cache (tile lists, scene choices): a frozen plan that "
                         "every machine then reads (the same scenes whenever a shard is built)")
    ap.add_argument("--selftest", action="store_true", help="build a synthetic pack (no network) and print its SHA-256 "
                                                            "(the same on every machine with the pinned Pillow)")
    a = ap.parse_args()
    if a.list_planet:
        sh = planet_shards()
        with open(a.out, "w") as fh:
            fh.write("".join(f"{x},{y}\n" for x, y in sh))
        print(f"{len(sh)} shards", flush=True)
        return 0
    if a.selftest:
        print(selftest(), flush=True)
        return 0
    cache = a.cache or os.path.join(a.out, "_stac")
    if a.pack:
        z, px, py = a.pack
        codes = tiles_over(pack_ll(z, px, py), cache)
        tiles = {c: tile_scenes(c, cache) for c in codes}
        (_, _, _), data, _, st = _photo_job((z, px, py, tiles, a.threads))
        _write(a.out, z, px, py, data, [])
        print(json.dumps({"pack": f"{z}/{px}/{py}", **st, "bytes": len(data or b""), "peak_mb": peak_mb()}))
        return 0
    if a.world:
        if not a.src:
            raise SystemExit("--world needs --from")
        build_world(a.out, a.src)
        return 0
    if a.coarse:
        if not a.src:
            raise SystemExit("--coarse needs --from")
        for x5, y5 in parse_xy(a.coarse, 32):
            build_coarse(a.out, a.src, x5, y5)
        return 0
    shards = parse_xy(a.shards, 1 << SHARD_Z)
    if a.shard_file:
        with open(a.shard_file) as fh:
            shards += parse_xy(fh.read(), 1 << SHARD_Z)
    if a.bbox:
        shards += shards_in(*a.bbox)
    seen, todo = set(), []
    for s in shards:
        if s not in seen:
            seen.add(s)
            todo.append(s)
    skip = set()
    if a.skip:
        with open(a.skip) as fh:
            for ln in fh:
                p = ln.split()
                if p:
                    skip.add(p[-1].split(PREFIX + "/", 1)[-1] if "/_build/" in p[-1] else p[-1])
    if a.stac_only:
        with ThreadPoolExecutor(4) as ex:
            n = sum(len(t) for t in ex.map(lambda xy: tiles_for_shard(*xy, cache), todo))
        print(f"{len(todo)} shards: {n} tile choices in {cache}", flush=True)
        return 0
    pool = None
    if a.workers > 1:
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing as mp
        pool = ProcessPoolExecutor(a.workers, mp_context=mp.get_context("spawn"), initializer=_worker_init)
    left = 0
    try:
        for x8, y8 in todo:
            rel = record_rel(x8, y8)
            if os.path.exists(os.path.join(a.out, rel)) or rel in skip or f"{PREFIX}/{rel}" in skip:
                print(f"shard {x8},{y8}: done before", flush=True)
                continue
            if a.deadline and time.time() > a.deadline:
                left += 1
                continue
            build_shard(a.out, x8, y8, a.workers, a.threads, cache, a.max_lat,
                        log=lambda s: print(s, flush=True), pool=pool)
    finally:
        if pool is not None:
            pool.shutdown()
    if left:
        print(f"{left} shards left for a later run (deadline)", flush=True)
        return 75
    return 0


if __name__ == "__main__":
    sys.exit(main())
