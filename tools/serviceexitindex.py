"""Offline OSM exit indexing, adapted from exitsigns.index_records (PR #363).

Kept independent of rendering and the application native module so the data
repository can build with only numpy and pyosmium. Parity is regression-tested.
"""
from collections import defaultdict
import math
import re
import numpy as np

APPROACH_M = 1800.


def labels(node, ramp, backwards=False):
    """Keep real posted labels; an interchange name is a last resort, never a city guess."""
    suffix = ':backward' if backwards else ':forward'
    def get(key):
        return ramp.get(key+suffix, ramp.get(key, ''))
    def values(text):
        out = []
        for part in re.split(r'[;|]', str(text or '')):
            part = ' '.join(''.join(c if c.isprintable() else ' ' for c in part).split())
            if part and part.casefold() not in {p.casefold() for p in out}:
                out.append(part[:96])
        return out
    ref = ' '.join(str(ramp.get('junction:ref', node.get('ref', node.get('junction:ref', '')))).split())[:24]
    if str(node.get('noref', '')).lower() == 'yes' and not ramp.get('junction:ref'):
        ref = ''
    names = values(get('destination')) or values(get('destination:street'))
    names = names or values(get('destination:lanes')) or values(node.get('exit_to')) or values(node.get('name'))
    routes = values(get('destination:ref')) or values(get('destination:ref:to')) or values(get('destination:ref:lanes'))
    return dict(ref=ref, names=names[:3], routes=routes[:3]) if ref or names or routes else None


def _direction(tags):
    value = tags.get('oneway', '')
    if value in ('-1', 'reverse'): return -1
    if value in ('no', '0', 'false'): return 0
    return 1 if value in ('yes', '1', 'true') or tags.get('highway') in ('motorway', 'motorway_link') else 0


def index_records(main_ways, links, coords, node_tags):
    """Join shared OSM nodes, excluding entries and private median/service connections.

    Coordinates are lon/lat. Only motorway/trunk vertices are retained by the
    caller, not the regional PBF's complete node population. Paths stop at an
    ambiguous upstream fork instead of jumping onto another carriageway.
    """
    incoming = defaultdict(list)
    for wid, refs, tags in main_ways:
        directions = (1, -1) if _direction(tags) == 0 else (_direction(tags),)
        for direction in directions:
            path = refs if direction == 1 else refs[::-1]
            for a, b in zip(path[:-1], path[1:]):
                if a in coords and b in coords and a != b:
                    incoming[b].append((a, wid, tags))
    def delta(a, b):
        p, q = np.asarray(coords[a]), np.asarray(coords[b])
        return (q-p)*[111320.*math.cos(math.radians(float((p[1]+q[1])/2))), 111320.]
    def unit(d): return d/max(float(np.linalg.norm(d)), 1e-9)
    out = []
    for rid, source, tags in links:
        if len(source) < 2 or tags.get('access') in ('private', 'no') or tags.get('service') == 'emergency_access':
            continue
        backwards = _direction(tags) == -1
        refs = source[::-1] if backwards else source
        nid = refs[0]
        if nid not in incoming or nid not in coords:
            continue                       # the other end is an on-ramp, not an exit
        label = labels(node_tags.get(nid, {}), tags, backwards)
        if label is None:
            continue
        ramp = [n for n in refs if n in coords]
        if len(ramp) < 2:
            continue
        rd = unit(delta(ramp[0], ramp[min(2, len(ramp)-1)]))
        candidates = [(float(np.matmul(unit(delta(a, nid)), rd)), a, wid, wt) for a, wid, wt in incoming[nid]]
        candidates.sort(key=lambda c: (-c[0], c[2], c[1]))
        if not candidates or candidates[0][0] < .15:
            continue
        _, previous, mid, mtags = candidates[0]
        approach, visited, length = [nid, previous], {nid, previous}, float(np.linalg.norm(delta(previous, nid)))
        main_ids = {mid}
        while length < APPROACH_M:
            at, after = approach[-1], approach[-2]
            d = unit(delta(at, after))
            choices = [(float(np.matmul(unit(delta(a, at)), d)), a, wid) for a, wid, _ in incoming[at]
                       if a not in visited]
            choices.sort(key=lambda c: (-c[0], c[2], c[1]))
            if not choices or choices[0][0] < .65 or (len(choices) > 1 and choices[0][0]-choices[1][0] < .12):
                break
            _, a, wid = choices[0]
            length += float(np.linalg.norm(delta(a, at)))
            approach.append(a); visited.add(a); main_ids.add(wid)
        # Retain enough of a split ramp to determine left/right even at a parallel tip.
        main_travel = unit(delta(previous, nid))
        right = np.array([main_travel[1], -main_travel[0]])
        offsets, run = [], 0.
        for a, b in zip(ramp[:-1], ramp[1:]):
            step = float(np.linalg.norm(delta(a, b)))
            if run+step > 150.:
                tip = delta(nid, a)+delta(a, b)*(150.-run)/max(step,1e-9)
                offsets.append(float(np.matmul(tip, right))); break
            offsets.append(float(np.matmul(delta(nid, b), right))); run += step
        # Use the first clear separation, before a loop ramp can turn across the highway.
        offset = next((v for v in offsets if abs(v) > 1.), max(offsets,key=abs,default=1.))
        side = 1 if offset >= 0 else -1
        extra = dict(label=label, approach=[coords[n] for n in approach[::-1]],
                     ramp=[coords[n] for n in ramp], main_ids=sorted(main_ids), ramp_id=rid,
                     main_id=mid, main_tags=mtags, ramp_tags=tags, side=side)
        # One row per outgoing ramp, including separately labelled branches at one junction.
        lo, la = coords[nid]
        out.append(dict(id=-rid, lon=lo, lat=la, tags=node_tags.get(nid, {}), ways=[extra], kind='exit'))
    return out



