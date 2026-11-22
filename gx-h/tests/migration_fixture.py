"""Install the genuine pre-epoch GX release into a disposable directory."""
import hashlib
from pathlib import Path
import subprocess


BASELINE = "23d581c27f46e65324246dff269b6ce77c9b3e9a"
BUNDLE = Path(__file__).resolve().parent / "fixtures" / "gx-23d581c.bundle"
BUNDLE_SHA256 = "0dd996d24c3c303c385b4d44869168b3f123d086a74adcf2c607aef669f02037"


def checkout_baseline(destination):
    if hashlib.sha256(BUNDLE.read_bytes()).hexdigest() != BUNDLE_SHA256:
        raise ValueError(f"Historical GX bundle checksum differs: {BUNDLE}")
    subprocess.run(["git", "clone", "--quiet", "--branch", "migration-baseline", "--no-checkout",
                    str(BUNDLE), str(destination)], check=True)
    subprocess.run(["git", "-C", str(destination), "checkout", "--quiet", "--detach", BASELINE], check=True)
    return destination
