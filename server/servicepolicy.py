"""Service-sign scenery defaults, shared by the offline builder and renderer."""
import json
from functools import lru_cache
from pathlib import Path

POLICY = Path(__file__).with_name('data') / 'service_sign_policy.json'


def policy(state, path=POLICY):
    path = Path(path)
    data = _data(str(path), path.stat().st_mtime_ns)
    return dict(data['default'], **data.get('states', {}).get(state, {}))


@lru_cache(maxsize=8)
def _data(path, stamp):
    return json.loads(Path(path).read_text(encoding='utf-8'))
