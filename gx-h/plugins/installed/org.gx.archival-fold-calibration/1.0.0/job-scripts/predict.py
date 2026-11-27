"""Local forward inference on the immutable, published Top7 calibration specimen."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


SPECIMEN = "12e199f65fa119dd141a674ab5881581964c267ff2c7b7770be2ebbefe52bfcf"
RUNNER = "f8aba1ff09cc34dc986a2a4533c79680d688c710c22da6b3ec727549c8cdb526"
WEIGHTS = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"


def digest(path):
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("specimen", "weight", "mount", "out"):
        p.add_argument("--" + key, type=Path, required=True)
    args = p.parse_args()
    if digest(args.specimen) != SPECIMEN:
        p.error("This calibration accepts only the registered 92-position Top7 input.")
    runner = args.mount / "source/run_alphafold.py"
    python = args.mount / "env/bin/python"
    if digest(runner) != RUNNER or digest(args.weight) != WEIGHTS:
        p.error("Local runner or parameter bytes differ from the registered calibration.")
    args.out.mkdir(parents=True, exist_ok=True)
    # Separate the single checked parameter file from other versions on the shared mount.
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        weights = work / "params"
        weights.mkdir()
        (weights / "af3.bin.zst").symlink_to(args.weight.resolve())
        request = work / "input.json"
        shutil.copyfile(args.specimen, request)
        result = work / "folds"
        cmd = [str(python), "-I", "-B", str(runner),
               "--json_path=" + str(request), "--model_dir=" + str(weights),
               "--output_dir=" + str(result), "--norun_data_pipeline",
               "--num_diffusion_samples=1", "--num_recycles=10", "--buckets=128"]
        env = dict(os.environ, XLA_PYTHON_CLIENT_PREALLOCATE="false")
        subprocess.run(cmd, check=True, env=env)
        folder = result / "published_top7_1qys"
        shutil.copyfile(folder / "published_top7_1qys_model.cif", args.out / "model.cif")
        shutil.copyfile(folder / "TERMS_OF_USE.md", args.out / "TERMS_OF_USE.md")
        confidences = json.loads((folder / "published_top7_1qys_summary_confidences.json").read_text())
    identity = subprocess.check_output([str(python), "-I", "-c",
        "import importlib.metadata as m,sys,json; print(json.dumps({'python':sys.version.split()[0],"
        "'jax':m.version('jax'),'jaxlib':m.version('jaxlib')}))"], text=True)
    record = {"calibration": "published_top7_1qys", "input_sha256": SPECIMEN,
              "runner_sha256": RUNNER, "weights_sha256": WEIGHTS, "weights_file": "af3.bin.zst",
              "environment": json.loads(identity), "model_sha256": digest(args.out / "model.cif"),
              "seed": 1, "recycles": 10, "samples": 1, "buckets": [128], "msa": "empty",
              "templates": [], "model_confidences": confidences, "output_terms": "TERMS_OF_USE.md"}
    (args.out / "inference.json").write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
