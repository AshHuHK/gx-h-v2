"""Run the local forward model on a versioned collection of natural food protein specimens."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


RUNNER_SHA = "f8aba1ff09cc34dc986a2a4533c79680d688c710c22da6b3ec727549c8cdb526"
WEIGHTS_SHA = "74d0258616917cd122f5eab6d076afe4a8930e96823851e65e4f777dfb1f33ff"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def request_for(specimen, collection):
    """Match the specimen's bytes to an independently curated mature chain."""
    if not isinstance(specimen, dict) or set(specimen) != {"accession", "sequence"}:
        raise ValueError("Supply one JSON object with accession and sequence.")
    acc, seq = specimen["accession"], specimen["sequence"]
    if not isinstance(acc, str) or not isinstance(seq, str) or not seq.isascii():
        raise ValueError("The natural specimen requires a registered accession and an ASCII sequence.")
    match = collection["specimens"].get(acc)
    if not match or hashlib.sha256(seq.encode()).hexdigest() != match["sequence_sha256"]:
        raise ValueError("The supplied sequence does not match this release's registered natural mature chain.")
    return {"name": acc.lower(), "modelSeeds": [1], "dialect": "alphafold3", "version": 3,
            "sequences": [{"protein": {"id": "A", "sequence": seq, "modifications": [],
                                       "unpairedMsa": "", "pairedMsa": "", "templates": []}}]}


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("specimen", "collection", "runner", "python", "weight", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    request = request_for(json.loads(args.specimen.read_bytes()), json.loads(args.collection.read_bytes()))
    if digest(args.runner) != RUNNER_SHA or digest(args.weight) != WEIGHTS_SHA:
        raise ValueError("Mounted runner or parameter digest does not match the registered method.")
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        weights = work / "parameters"
        weights.mkdir()
        (weights / "af3.bin.zst").symlink_to(args.weight.resolve())
        source = work / "input.json"
        source.write_text(json.dumps(request))
        result = work / "result"
        bucket = 128 if len(request["sequences"][0]["protein"]["sequence"]) <= 128 else 256
        command = [str(args.python), "-I", "-B", str(args.runner), "--json_path=" + str(source),
                   "--model_dir=" + str(weights), "--output_dir=" + str(result), "--norun_data_pipeline",
                   "--num_diffusion_samples=1", "--num_recycles=10", "--buckets=" + str(bucket)]
        env = dict(os.environ, XLA_PYTHON_CLIENT_PREALLOCATE="false")
        with (args.out / "provider-events.jsonl").open("a") as journal:
            with subprocess.Popen(command, env=env) as child:
                # Linux start ticks disambiguate PID reuse when the outer Driver is restarted.
                stat = Path(f"/proc/{child.pid}/stat").read_text().rsplit(")", 1)[1].split()
                launch = {"event": "accepted", "utc": utc(), "pid": child.pid, "start_ticks": stat[19],
                          "device_assignment": env.get("CUDA_VISIBLE_DEVICES"), "model_input_sha256": digest(source)}
                journal.write(json.dumps(launch) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                code = child.wait()
                journal.write(json.dumps({"event": "exit", "utc": utc(), "pid": child.pid, "code": code}) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                if code:
                    raise RuntimeError(f"The registered forward model exited with status {code}.")
        name = request["name"]
        folder = result / name
        shutil.copyfile(folder / f"{name}_model.cif", args.out / "model.cif")
        shutil.copyfile(folder / "TERMS_OF_USE.md", args.out / "TERMS_OF_USE.md")
        confidences = json.loads((folder / f"{name}_summary_confidences.json").read_bytes())
    versions = subprocess.check_output([str(args.python), "-I", "-c",
        "import importlib.metadata as m,sys,json; print(json.dumps({'python':sys.version.split()[0],"
        "'jax':m.version('jax'),'jaxlib':m.version('jaxlib')}))"], text=True)
    record = {"model": "AlphaFold 3", "accession": name.upper(), "input_sha256": digest(args.specimen),
              "registered_collection_sha256": digest(args.collection), "runner_sha256": RUNNER_SHA,
              "weights_sha256": WEIGHTS_SHA, "model_sha256": digest(args.out / "model.cif"),
              "seed": 1, "recycles": 10, "samples": 1, "bucket": bucket, "msa": "empty", "templates": [],
              "environment": json.loads(versions), "summary_confidences": confidences, "launch": launch}
    (args.out / "inference.json").write_text(json.dumps(record, indent=2) + "\n")


if __name__ == "__main__":
    main()
