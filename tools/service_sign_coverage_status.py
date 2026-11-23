"""Read service-sign build progress without changing the build or game."""
import argparse
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=root/'build/service-signs-us')
    args = parser.parse_args()
    path = args.work/'status.json'
    status = json.loads(path.read_text()) if path.exists() else dict(phase='not_started')
    partial = args.work/'us-latest.osm.pbf.part'
    if partial.exists():
        with partial.open('rb') as stream:
            stream.seek(0, 2)
            status['downloaded_bytes'] = stream.tell()
    status['progress_log'] = str(args.work/'build-error.log')
    print(json.dumps(status, indent=2))


if __name__ == '__main__': main()
