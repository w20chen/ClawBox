#!/usr/bin/env python3
import glob
import os

root = "/data/recording-evidence/session-0000"
needles = (b"call00V5kd9SryyUCmfDYlwvL06737", b"/dev/fd/3")
for path in glob.glob(root + "/**/*", recursive=True):
    if not os.path.isfile(path) or os.path.getsize(path) > 20_000_000:
        continue
    try:
        payload = open(path, "rb").read()
    except OSError:
        continue
    matched = [needle.decode() for needle in needles if needle in payload]
    if matched:
        print(path, os.path.getsize(path), matched)

print("TOP LEVEL")
for path in sorted(glob.glob(root + "/*")):
    print(path, os.path.getsize(path) if os.path.isfile(path) else "DIR")
