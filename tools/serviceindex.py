"""Offline, conservative OSM exit/service routing. No road searches in gameplay.

Only explicit network access qualifies: a business node on a drivable way, or
a drivable vertex inside its mapped area. We never join a POI to the nearest
road across an unknown fence. Restrictions are handled conservatively by
blocking the via node (including via-way nodes) rather than guessing a turn.
"""
from collections import defaultdict
import heapq
import math

import serviceexitindex as exitsigns
import signstates
import servicefiction
from servicepolicy import policy, POLICY

CATEGORIES = {'restaurant': 'FOOD', 'fast_food': 'FOOD', 'cafe': 'FOOD',
              'fuel': 'GAS', 'hotel': 'LODGING', 'motel': 'LODGING',
              'guest_house': 'LODGING', 'camp_site': 'CAMPING',
              'caravan_site': 'CAMPING', 'hospital': 'HOSPITAL'}
DRIVABLE = {'motorway', 'motorway_link', 'trunk', 'trunk_link', 'primary',
             'primary_link', 'secondary', 'secondary_link', 'tertiary',
             'tertiary_link', 'unclassified', 'residential', 'service', 'living_street'}


def allowed(tags):
    access = next((tags[k] for k in ('motorcar', 'motor_vehicle', 'vehicle', 'access') if k in tags), 'yes')
    return (access in ('yes', 'permissive', 'designated', 'destination', 'customers')
            and not any(':conditional' in k for k in tags)
            and tags.get('service') != 'emergency_access'
            and tags.get('area') != 'yes')


def controlled(tags):
    return tags.get('highway') == 'motorway' or (tags.get('highway') == 'trunk' and
           (tags.get('motorroad') == 'yes' or tags.get('expressway') == 'yes'))


def metres(a, b):
    return math.hypot((a[0]-b[0])*111320*math.cos(math.radians((a[1]+b[1])/2)),
                      (a[1]-b[1])*111320)


def inside(point, ring):
    if point in ring: return True
    x, y = point
    result = False
    for (ax, ay), (bx, by) in zip(ring, ring[1:]+ring[:1]):
        if (ay > y) != (by > y) and x < ax+(y-ay)*(bx-ax)/(by-ay):
            result = not result
    return result


def area_rings(element, coords, role='outer'):
    """Closed ways and stitched multipolygon outer members; holes omitted conservatively."""
    if element['type'] == 'way':
        if role == 'inner': return []
        refs = element.get('nodes', [])
        if refs and all(n in coords for n in refs):
            pieces = [[coords[n] for n in refs]]
        else:
            pieces = [[[p['lon'], p['lat']] for p in element.get('geometry', []) if p]]
    elif element['type'] == 'relation':
        pieces = [[[p['lon'],p['lat']] for p in m.get('geometry', []) if p]
                  for m in element.get('members', []) if (m.get('role') == role or (role == 'outer' and m.get('role') == '')) and m['type'] == 'way']
    else:
        return []
    pending, rings = [p for p in pieces if len(p) > 1], []
    while pending:
        line = pending.pop(0)
        while line[-1] != line[0]:
            match = next(((i, p if p[0] == line[-1] else p[::-1]) for i,p in enumerate(pending)
                          if p[0] == line[-1] or p[-1] == line[-1]), None)
            if match is None: break
            i, piece = match
            pending.pop(i)
            line.extend(piece[1:])
        if len(line) >= 4 and line[0] == line[-1]: rings.append(line)
    return rings


def build(data, policy_path=POLICY, owner=None):
    elements = data['elements']
    coords, nodes, ways = {}, {}, {}
    for e in elements:
        if e['type'] == 'node':
            coords[e['id']] = [e['lon'], e['lat']]
            nodes[e['id']] = e.get('tags', {})
        elif e['type'] == 'way':
            ways[e['id']] = (e.get('nodes', []), e.get('tags', {}))
            for nid, p in zip(e.get('nodes', []), e.get('geometry', [])):
                if p:
                    coords[nid] = [p['lon'], p['lat']]
    blocked = {n for n, t in nodes.items() if t.get('barrier') and
               (t.get('barrier') not in ('gate', 'lift_gate', 'cattle_grid') or not allowed(t)
                or not any(t.get(k) in ('yes', 'permissive', 'customers')
                           for k in ('motorcar', 'motor_vehicle', 'vehicle', 'access')))}
    restricted = []
    barriers = []
    for e in elements:
        tags = e.get('tags', {})
        geometry = e.get('geometry', [])
        if tags.get('barrier') and e['type'] == 'way' and geometry:
            barriers.append([[p['lon'], p['lat']] for p in geometry if p])
        if tags.get('landuse') == 'military' or tags.get('military') in ('base', 'barracks', 'airfield'):
            rings = area_rings(e, coords)
            if not rings and e['type'] == 'relation':
                # Incomplete mapped base boundaries fail closed. Their enclosing
                # box can omit public POIs, but must never advertise base access.
                points = [p for m in e.get('members', []) if m.get('role') == 'outer'
                          for p in m.get('geometry', []) if p]
                if points:
                    x0,x1 = min(p['lon'] for p in points),max(p['lon'] for p in points)
                    y0,y1 = min(p['lat'] for p in points),max(p['lat'] for p in points)
                    rings = [[[x0,y0],[x1,y0],[x1,y1],[x0,y1],[x0,y0]]]
            restricted.extend(rings)
    for ring in restricted:
        lo = [min(p[i] for p in ring) for i in (0,1)]
        hi = [max(p[i] for p in ring) for i in (0,1)]
        blocked.update(n for n,p in coords.items() if all(lo[i] <= p[i] <= hi[i] for i in (0,1)) and inside(p,ring))
    for e in elements:
        if e['type'] == 'relation' and e.get('tags', {}).get('type') == 'restriction':
            for m in e.get('members', []):
                if m.get('role') == 'via':
                    blocked.update([m['ref']] if m['type'] == 'node' else ways.get(m['ref'], ([], {}))[0])
    graph, road_nodes = defaultdict(list), set()
    for wid, (refs, tags) in sorted(ways.items()):
        cls = tags.get('highway')
        if cls not in DRIVABLE or not allowed(tags):
            continue
        direction = exitsigns._direction(tags)
        if tags.get('junction') == 'roundabout' and 'oneway' not in tags:
            direction = 1
        for a, b in zip(refs, refs[1:]):
            if a not in coords or b not in coords or a in blocked or b in blocked:
                continue
            # Local access must never re-enter a motorway to reach another exit.
            if controlled(tags):
                continue
            if not cls.endswith('_link') and tags.get('bridge') not in ('yes', 'viaduct') and tags.get('tunnel') != 'yes' and tags.get('layer', '0') == '0':
                road_nodes.update((a, b))
            distance = metres(coords[a], coords[b])
            if direction >= 0: graph[a].append((b, distance, wid))
            if direction <= 0: graph[b].append((a, distance, wid))
    businesses = []
    by_node = defaultdict(list)
    node_grid = defaultdict(list)
    for n in sorted(road_nodes-blocked):
        node_grid[tuple(math.floor(v*200) for v in coords[n])].append(n)
    for e in sorted(elements, key=lambda e: (e['type'], e['id'])):
        tags = e.get('tags', {})
        category = CATEGORIES.get(tags.get('amenity')) or CATEGORIES.get(tags.get('tourism'))
        if not category or not allowed(tags) or tags.get('disused') == 'yes':
            continue
        access = []
        if e['type'] == 'node' and e['id'] in road_nodes and e['id'] not in blocked:
            access = [e['id']]
        elif e['type'] in ('way', 'relation'):
            refs = e.get('nodes', [])
            holes = area_rings(e, coords, 'inner')
            for ring in area_rings(e, coords):
                lo = [min(p[i] for p in ring) for i in (0, 1)]
                hi = [max(p[i] for p in ring) for i in (0, 1)]
                nearby = [n for x in range(math.floor(lo[0]*200), math.floor(hi[0]*200)+1)
                          for y in range(math.floor(lo[1]*200), math.floor(hi[1]*200)+1)
                          for n in node_grid.get((x,y), ())]
                access.extend(n for n in nearby if (n in refs or inside(coords[n], ring))
                              and not any(inside(coords[n], hole) for hole in holes))
        if not access:
            continue
        business = servicefiction.business(dict(id=f"osm:{e['type'][0]}{e['id']}", category=category,
                        source=f"https://www.openstreetmap.org/{e['type']}/{e['id']}"))
        businesses.append(business)
        for n in access: by_node[n].append(len(businesses)-1)
    main, links = [], []
    for wid, (refs, tags) in sorted(ways.items()):
        if tags.get('highway') in ('motorway_link', 'trunk_link'): links.append((wid, refs, tags))
        if not allowed(tags): continue
        if controlled(tags): main.append((wid, refs, tags))
    indexed = {r['ways'][0]['ramp_id']: r for r in exitsigns.index_records(main, links, coords, nodes)}
    incoming = set()
    for _, refs, tags in main:
        direction = exitsigns._direction(tags)
        if direction >= 0: incoming.update(refs[1:])
        if direction <= 0: incoming.update(refs[:-1])
    records, checks = [], []
    for rid, refs, tags in links:
        directed = refs[::-1] if exitsigns._direction(tags) == -1 else refs
        if not directed or directed[0] not in coords:
            raise ValueError(f'Missing exit coordinates on ramp {rid}')
        lon, lat = coords[directed[0]]
        if owner is not None and (math.floor(lon * 2), math.floor(lat * 2)) != owner:
            continue
        state = str(signstates.states(lat, lon).item())
        check = dict(ramp_id=rid, lon=lon, lat=lat, state=state, status='excluded', categories={})
        checks.append(check)
        if not state:
            check['reason'] = 'outside_us'
            continue
        if not allowed(tags):
            check['reason'] = 'restricted_ramp_access'
            continue
        if directed[0] not in incoming:
            check['reason'] = 'not_an_outgoing_freeway_ramp'
            continue
        record = indexed.get(rid)
        if record is None:
            label = exitsigns.labels(nodes.get(directed[0], {}), tags, exitsigns._direction(tags) == -1)
            check['status'] = 'excluded' if not label or not label['ref'] else 'unresolved'
            check['reason'] = 'unnumbered_exit' if check['status'] == 'excluded' else 'approach_geometry'
            continue
        extra = record['ways'][0]
        if not state or not extra['label']['ref']:
            check['reason'] = 'unnumbered_exit'
            continue
        rules = policy(state, policy_path)
        rid = extra['ramp_id']
        refs, tags = ways[rid]
        refs = refs[::-1] if exitsigns._direction(tags) == -1 else refs
        # Start on this outgoing ramp, and retain every metre of its cost.
        start = refs[0]
        limit = max(rules['distance_m'].values())
        queue, distances, found = [(0., start)], {start: 0.}, {}
        previous = {}
        while queue:
            distance, at = heapq.heappop(queue)
            if distance != distances[at]: continue
            for index in by_node.get(at, ()):
                b = businesses[index]
                if distance <= rules['distance_m'].get(b['category'], -1):
                    if b['id'] not in found:
                        at_path, route = at, []
                        while at_path in previous:
                            before, way = previous[at_path]
                            route.append(way)
                            at_path = before
                        route.reverse()
                        route = [w for i, w in enumerate(route) if i == 0 or w != route[i-1]]
                        found[b['id']] = dict(b, distance_m=round(distance, 1), access_node=at, route_way_ids=route)
            for to, length, wid in graph.get(at, ()):
                if at == start and wid != rid: continue
                cost = distance+length
                if cost <= limit and cost < distances.get(to, math.inf):
                    distances[to] = cost
                    previous[to] = at, wid
                    heapq.heappush(queue, (cost, to))
        if not found:
            check['status'] = 'no_services'
            continue
        check['status'] = 'with_services'
        for b in found.values():
            check['categories'][b['category']] = check['categories'].get(b['category'], 0) + 1
        services = sorted(found.values(), key=lambda b: (b['category'], b['distance_m'], b['id']))
        extra['services'] = services
        extra['service_state'] = state
        approach = extra['approach']
        pad = .001
        lo = [min(p[i] for p in approach)-pad for i in (0,1)]
        hi = [max(p[i] for p in approach)+pad for i in (0,1)]
        extra['service_barriers'] = [line for line in barriers if line and
            all(max(p[i] for p in line) >= lo[i] and min(p[i] for p in line) <= hi[i] for i in (0,1))]
        # Blue service boards need the exit number and geometry, not business destinations.
        extra['label'] = dict(ref=extra['label']['ref'], names=[], routes=[])
        extra['main_tags'] = {k: v for k, v in extra['main_tags'].items() if k in ('highway', 'oneway', 'lanes', 'ref')}
        extra['ramp_tags'] = {k: v for k, v in extra['ramp_tags'].items() if k in ('highway', 'oneway', 'lanes')}
        records.append(dict(id=f"service:{rid}", lon=record['lon'], lat=record['lat'], tags={},
                            exit=extra, provenance='inferred'))
    return dict(format=1, attribution='© OpenStreetMap contributors, ODbL 1.0',
                osm_timestamp=data.get('osm3s', {}).get('timestamp_osm_base'), objects=records,
                name_origin='fictional', naming_version=servicefiction.VERSION, audit=checks,
                diagnostics=dict(elements=len(elements), connected_businesses=len(businesses), exits_with_services=len(records)))


