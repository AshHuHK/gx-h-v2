"""Registered signed deletion effects and matched dispersed-deletion controls in a deposited human PCNA trimer."""
import argparse
import importlib.util
import json
from pathlib import Path
from statistics import median
import sys

# The old deletion engine uses three positional role names internally; expose the actual label chains in every product.
ENGINE_PAIRS = {"12":"AB","23":"BC","31":"AC"}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def atom_keys(selection, mask):
    coords = {}
    observed = set()
    for role,label in zip("ABC",selection.chains):
        atoms = {}
        for residue in selection.molecules[label]:
            pos = residue.id[1]
            for atom in residue:
                if atom.name in ("N","CA","C","O"):
                    observed.add(pos)
                    atoms[pos,residue.resname,atom.name] = tuple(float(v) for v in atom.coord)
        # Ambiguous sites remain observed even though they cannot supply matched geometry.
        observed.update(selection.excluded_residues[label])
        coords[role] = atoms
    common = set.intersection(*(set(a) for a in coords.values()))
    full = {key[:2] for key in common if all((*key[:2],name) in common for name in ("N","CA","C","O"))}
    keys = sorted(k for k in common if (k[2]=="CA" if mask=="common_ca" else k[:2] in full))
    return coords,keys,observed


def readout(before, after):
    return {"before":before,"after":after,"delta":None if before is None or after is None else after-before}


def middle(values):
    return None if None in values else median(values)


def physical_table(selection, mask, endpoint, engine, fitter):
    coords,keys,observed = atom_keys(selection,mask)
    positions = sorted({k[0] for k in keys})
    baseline = engine.fits(coords,keys,fitter)
    indices = range((max(selection.declared_positions)+19)//20)
    original = engine.assess_blocks(positions,keys,coords,baseline,selection.input_sha256,mask,indices,observed,fitter)
    selected_base = {p:baseline[alias][endpoint] for p,alias in ENGINE_PAIRS.items()}
    blocks = []
    for b in original:
        block = {"block_index":b["index"],"interval":b["interval"],"eligible":b["eligible"],"reasons":b["reasons"],
                 "omitted_positions":b["positions"],"retained_count":b["retained_count"],"pairs":{},"controls":[]}
        if b["eligible"]:
            for control in b["controls"]:
                block["controls"].append({"k":control["k"],"omitted_positions":control["positions"],
                                          "pair_deltas":{p:readout(selected_base[p],control["pairs"][alias][endpoint])["delta"]
                                                         for p,alias in ENGINE_PAIRS.items()}})
            for p,alias in ENGINE_PAIRS.items():
                values = readout(selected_base[p],b["pairs"][alias][endpoint])
                distribution = [abs(c["pair_deltas"][p]) for c in block["controls"] if c["pair_deltas"][p] is not None]
                values.update(control_count=len(distribution),control_max=max(distribution) if len(distribution)==20 else None)
                block["pairs"][p] = values
            block["median"] = readout(middle(list(selected_base.values())),middle([v["after"] for v in block["pairs"].values()]))
        blocks.append(block)
    return {"schema":"gx.pcna-geometry-table/1", "method":"pcna-local-geometry@1","entry":selection.entry,
            "accession":"P12004","model":selection.model,"entity":selection.entity,"chains":list(selection.chains),
            "pair_labels":{k:[selection.chains[int(x)-1] for x in k] for k in ENGINE_PAIRS},
            "input_sha256":selection.input_sha256,"mask":mask,"endpoint":endpoint,
            "matched_keys_sha256":fitter.object_digest(keys),"matched_atoms":len(keys),"selected_residues":len(positions),
            "observed_union_residues":len(observed),"matched_positions":positions,
            "declared_positions":list(selection.declared_positions),"reference_alignments":list(selection.reference_alignments),
            "selected_residue_altlocs":selection.alternate_residues,"excluded_residues":selection.excluded_residues,
            "baseline":selected_base,"blocks":blocks}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("structure","selection","metrics","engine","fitter","out"):
        cli.add_argument("--"+name,type=Path,required=True)
    for name in ("mask","endpoint"):
        cli.add_argument("--"+name,required=True)
    cli.add_argument("--effect-minimum",type=float,required=True)
    cli.add_argument("--zero-limit",type=float,required=True)
    cli.add_argument("--minimum-evaluable",type=int,required=True)
    cli.add_argument("--pair",default="12")
    cli.add_argument("--block-index",type=int,default=-1)
    args = cli.parse_args()
    params = {k:getattr(args,k) for k in ("mask","endpoint","effect_minimum","zero_limit","minimum_evaluable","pair","block_index")}
    metrics = load("pcna_geometry_metrics",args.metrics)
    metrics.validate(params)
    selection = load("pcna_geometry_selection",args.selection).select(args.structure.read_bytes())
    engine = load("released_block_influence",args.engine)
    physical = physical_table(selection,args.mask,args.endpoint,engine,engine.load_fitter(args.fitter))
    assessment = metrics.assess(physical,params,include_rows=True)
    for block,annotation in zip(physical["blocks"],assessment.pop("annotations"),strict=True):
        block.update(annotation)
    physical["assessed_parameters"] = params
    scope = {k:physical[k] for k in ("entry","accession","model","entity","chains","pair_labels","input_sha256","matched_keys_sha256")}
    summary = {**scope,"schema":"gx.pcna-geometry/1","method":metrics.METHOD,**params,
               "unit":metrics.UNITS[args.endpoint],"delta_tolerance":metrics.TOLERANCES[args.endpoint],**assessment}
    args.out.mkdir(parents=True,exist_ok=True)
    for filename,data in (("geometry.json",summary),("geometry-table.json",physical)):
        (args.out/filename).write_text(json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(",",":"))+"\n")


if __name__ == "__main__":
    main()
