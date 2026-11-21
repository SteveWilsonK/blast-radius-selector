"""Download sqlglot at the pinned commit from its own repository and unpack it."""
import io
import os
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"

dest = sys.argv[1]
data = urllib.request.urlopen(URL, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
    prefix = f"sqlglot-{COMMIT}/"
    for member in archive.getmembers():
        if not member.name.startswith(prefix):
            continue
        member.name = member.name[len(prefix):]
        if member.name:
            archive.extract(member, dest, filter="data")
print(f"sqlglot {COMMIT} unpacked into {dest}: {len(os.listdir(dest))} entries")
