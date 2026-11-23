#!/usr/bin/env python3
"""safe_tar.py TAR FOLDER: exit 0 when every entry of the tar is a regular file or a directory under FOLDER/ (or
FOLDER itself), with a relative path and no "." / ".." / empty part; exit 1 otherwise (the entries refused printed),
before anything of it is unpacked. Links, devices, FIFOs, absolute paths, ../ paths and entries of other datasets
(rail/manifest.json) would overwrite the site the mirror step uploads. Standard library only.

    python3 tools/safe_tar.py parts/dedecks.tar dedecks && tar xf parts/dedecks.tar -C site dedecks
"""
import sys
import tarfile


def refused(path, folder):
    """The entries of tar path that are not a regular file or directory under folder/ ([] none; a tar that cannot be
    read is refused as a whole)."""
    bad = []
    try:
        with tarfile.open(path) as t:
            for m in t:
                name = m.name
                parts = name.split("/")
                if (not (m.isreg() or m.isdir()) or name.startswith("/") or "\\" in name or "\0" in name
                        or parts[0] != folder or (len(parts) == 1 and not m.isdir())
                        or any(p in ("", ".", "..") for p in parts)):
                    bad.append(f"{name} ({'file' if m.isreg() else 'dir' if m.isdir() else 'link or device'})")
    except (OSError, tarfile.TarError) as e:
        return [f"unreadable: {e}"]
    return bad


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    got = refused(sys.argv[1], sys.argv[2])
    for b in got[:20]:
        print(f"{sys.argv[1]}: refused entry {b}")
    if got:
        print(f"{sys.argv[1]}: {len(got)} entries not regular files or folders under {sys.argv[2]}/: not unpacked")
    sys.exit(1 if got else 0)
