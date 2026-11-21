"""Read a compressed pickle, or the plain copy the image build leaves beside it."""
import os
import pickle
import zlib


def load(path):
    raw = path + ".raw"
    if os.path.exists(raw):
        with open(raw, "rb") as handle:
            return pickle.loads(handle.read())
    with open(path, "rb") as handle:
        return pickle.loads(zlib.decompress(handle.read()))


if __name__ == "__main__":
    # python -m br.blob <file>...: write the plain copies (run while the image builds).
    import sys

    for name in sys.argv[1:]:
        with open(name, "rb") as handle:
            data = zlib.decompress(handle.read())
        with open(name + ".raw", "wb") as handle:
            handle.write(data)
