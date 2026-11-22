"""Restore a wheel from its unpacked files, retaining and checking the original RECORD."""
import argparse
import base64
import csv
import hashlib
from pathlib import Path
import zipfile


def pack(tree, output):
    record, = tree.glob("*.dist-info/RECORD")
    with record.open(newline="") as stream:
        rows = {name: (digest, size) for name, digest, size in csv.reader(stream)}
    files = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
    if files != rows.keys():
        raise ValueError("Wheel files differ from the original RECORD")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as wheel:
        for name in sorted(files):
            path = tree / name
            data = path.read_bytes()
            sha = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            if path != record and rows[name] != ("sha256=" + sha, str(len(data))):
                raise ValueError(f"Wheel member differs from RECORD: {name}")
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
            wheel.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    return len(files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel_tree", type=Path)
    parser.add_argument("output_wheel", type=Path)
    args = parser.parse_args()
    print(pack(args.wheel_tree, args.output_wheel), "verified files")
