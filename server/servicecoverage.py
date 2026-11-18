"""Bounded, local-only reader for progressively published national service cells."""
from collections import OrderedDict
import gzip
import hashlib
import json
import math
from pathlib import Path
import threading
import io
import re
import time
import logging

CELL = .5
MAX_CACHE_BYTES = 128*1024*1024
MAX_COMPRESSED = 8*1024*1024
MAX_BODY = 32*1024*1024


def cell(lon, lat):
    return math.floor(float(lon)/CELL), math.floor(float(lat)/CELL)


def key(x, y):
    return f'{x}_{y}'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.part')
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')), encoding='utf-8')
    temporary.replace(path)


class Source:
    def __init__(self, directory=None, *, cache_dir=None, fetcher=None, background=False):
        self.remote = None
        self.background = background
        self.requested_at = -float('inf')
        if directory is None:
            import pagesdata
            checked = _ManifestFetcher(fetcher) if fetcher is not None else None
            self.remote = pagesdata.Manifest(cache_dir, checked, 'services', key=manifest_key)
            directory = self.remote.dir
        self.directory = Path(directory).resolve()
        self.manifest = {}
        self.stamp = None
        self.wanted = OrderedDict()
        self.cache = OrderedDict()
        self.lock = threading.RLock()

    def version(self):
        with self.lock:
            if self.remote is not None:
                self.remote.known()
                if self.remote.manifest is None:
                    return None
                self.manifest = self.remote.manifest
                # Published builds are transactional. A common build identity keeps
                # the pose handler and all road workers on the same cache key.
                return self.remote.built
            path = self.directory/'manifest.json'
            try:
                stat = path.stat()
                stamp = stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_ctime_ns
                if stamp != self.stamp:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    if data.get('format') != 1 or data.get('cell_degrees') != CELL or not isinstance(data.get('cells'), dict):
                        raise ValueError('Unsupported national service coverage manifest')
                    self.manifest, self.stamp = data, stamp
                selected = {k:self.manifest['cells'].get(k, {}).get('sha256') for k in sorted(self.wanted)}
                return hashlib.sha256(json.dumps([self.manifest.get('built'), selected], sort_keys=True).encode()).hexdigest()
            except FileNotFoundError:
                self.manifest, self.stamp = {}, None
                return None

    def query(self, lon0, lat0, lon1, lat1):
        with self.lock:
            if self.remote is not None:
                if self.background and self.remote.known() is None:
                    if time.monotonic() - self.requested_at >= self.remote.retry_s:
                        self.requested_at = time.monotonic()
                        threading.Thread(target=self._warm, name='service-data-manifest', daemon=True).start()
                    import revisit
                    revisit.fault(here=True)
                    return [], set()
                self.remote.current()
            self.version()
            cells = self.manifest.get('cells', {})
            x0,y0 = cell(lon0,lat0)
            x1,y1 = cell(lon1,lat1)
            if (x1-x0+1)*(y1-y0+1) > 128:
                raise ValueError('Service coverage query is too large')
            requested = [key(x,y) for x in range(x0,x1+1) for y in range(y0,y1+1)]
            records, covered = [], set()
            for k in requested:
                self.wanted[k] = None
                self.wanted.move_to_end(k)
                while len(self.wanted) > 64: self.wanted.popitem(last=False)
                entry = cells.get(k)
                if entry is None: continue
                digest = entry['sha256']
                cached = self.cache.get(k)
                if cached is None or cached[0] != digest:
                    relative = entry['path']
                    parts = relative.split('/')
                    if (relative.startswith('/') or '\\' in relative or
                            any(p in ('', '.', '..') or not re.fullmatch('[0-9A-Za-z_.-]+', p) for p in parts)):
                        raise ValueError('Coverage path leaves dataset')
                    if not re.fullmatch('[0-9a-f]{64}', digest):
                        raise ValueError('Invalid service coverage digest')
                    path = (self.directory/relative).resolve()
                    if not path.is_relative_to(self.directory): raise ValueError('Coverage path leaves dataset')
                    if self.remote is not None:
                        path = self.directory/'cells'/(digest+'.json.gz')
                    try:
                        with path.open('rb') as f:
                            raw = f.read(MAX_COMPRESSED+1)
                        if len(raw) > MAX_COMPRESSED or hashlib.sha256(raw).hexdigest() != digest:
                            raise ValueError('Service coverage checksum mismatch')
                    except FileNotFoundError:
                        if self.remote is None: raise
                        raw = self.remote.fetch(relative)
                        if len(raw) > MAX_COMPRESSED or hashlib.sha256(raw).hexdigest() != digest:
                            raise ValueError('Service coverage checksum mismatch')
                        # Decode and validate before saving downloaded content.
                    if hashlib.sha256(raw).hexdigest() != digest: raise ValueError('Service coverage checksum mismatch')
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                        body = f.read(MAX_BODY+1)
                    if len(body) > MAX_BODY: raise ValueError('Service coverage cell is too large')
                    data = json.loads(body)
                    if data.get('format') != 1 or not isinstance(data.get('objects'), list):
                        raise ValueError('Unsupported service coverage cell')
                    if self.remote is not None:
                        import pagesdata
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if not path.exists(): pagesdata.write_atomic(str(path), raw)
                    # Reserve for decoded Python dictionaries/lists as well as
                    # source text; large cells can be used without being retained.
                    cached = digest, data['objects'], 6*len(body)
                    self.cache[k] = cached
                self.cache.move_to_end(k)
                while len(self.cache) > 16 or sum(v[2] for v in self.cache.values()) > MAX_CACHE_BYTES:
                    self.cache.popitem(last=False)
                covered.add(k)
                records.extend(o for o in cached[1] if lon0 <= o['lon'] <= lon1 and lat0 <= o['lat'] <= lat1)
            return records, covered

    def _warm(self):
        try: self.remote.current()
        except Exception as error:
            logging.getLogger('earthd.servicesigns').debug('Service manifest unavailable: %s', error)


def manifest_key(manifest):
    """Only whole audited builds may become a network dataset."""
    if (manifest.get('format') != 1 or manifest.get('cell_degrees') != CELL
            or manifest.get('complete') is not True or manifest.get('name_origin') != 'fictional'
            or not isinstance(manifest.get('cells'), dict) or not manifest.get('built')):
        raise ValueError('Unsupported or incomplete service dataset')
    return manifest['built']


class _ManifestFetcher:
    """Validate a new manifest before pagesdata pins it over the offline copy."""
    def __init__(self, fetcher):
        self.fetcher = fetcher

    def get(self, url, **kwargs):
        response = self.fetcher.get(url, **kwargs)
        if url.endswith('/manifest.json'):
            if len(response.body) > 4*1024*1024: raise ValueError('Service manifest is too large')
            manifest_key(json.loads(response.body))
        return response

