# TL1A chain geometry in PDB 2RE9

The two CPU capabilities measure how the fitted geometry of the three observed TL1A chains changes with the selection of atoms. The supplied X-ray model has 2.1 Å resolution. Label entity 1 contains 181 declared residue positions. Positions 1–17 have no observed backbone in any chain. The frozen coverage denominator is the union of **164 observed residue positions** across chains A, B and C. [Deposited coordinates](data/2RE9.cif) · [Origin, revision and SHA-256](data/provenance.json) · [Structure paper](https://doi.org/10.1016/j.bbrc.2007.09.097)

## Inputs and outputs

`tl1a-coordinate-quality@1` reads the original, uncompressed 2RE9 mmCIF. Its `quality.json` contains positive-occupancy backbone counts per chain, the three-chain intersection, the observed union, entirely unobserved polymer positions, and the exact sorted atom identities in each selection. A position refers to `label_seq_id`. A matched atom also requires the same `label_comp_id` and `label_atom_id` in model 1 on each chain. Deposited Cartesian coordinates are used without applying assembly or crystal operators. [Quality capability](quality.json)

`tl1a-geometry@1` consumes the original coordinates, frozen criteria, and a selected protocol. In a composed investigation it takes the measured quality inventory as a Job input, gathers those exact mapped atoms, and records the inventory hash. Direct execution can construct the atom selection from the coordinate input. All three fixed/moving pairs use the same atoms, weighted equally. A centered, least-squares proper rotation and translation gives the unsigned rotation angle and fitted Cartesian atom RMSD. `geometry.json` preserves fitted values even when the selected region falls below the criterion for drawing a conclusion. [Geometry capability](geometry.json) · [Worker](worker.py) · [Frozen criteria](data/criteria.json)

| Protocol | Selection | Observed result |
|---|---|---|
| `common_backbone` | Shared N, CA, C and O atom identities | 656 atoms at 164/164 observed positions |
| `common_ca` | Shared CA identities | 164 atoms at 164/164 positions |
| `ordered_backbone` | Complete N, CA, C and O on all three chains, occupancy 1 and B factor ≤ 40 Å² for each of the twelve observations | 196 atoms at 49/164 positions |

## Reference measurements

The production worker solves Horn’s unit-quaternion eigensystem with the Python standard library. The reference rereads the original mmCIF with Biopython’s `MMCIF2Dict`, constructs the atom masks independently and calls `Bio.SVDSuperimposer` with NumPy SVD. It checks the mapped-key hash, counts, coverage, units, fit eligibility, and every pair’s angle, RMSD and angular departure from 120°. The numerical reproduction tolerances are 0.01° and 0.001 Å. [Independent reference implementation](review.py) · [Numeric reference and dependency versions](data/reference.json)

| Fixed / moving | Common backbone angle / RMSD | Common CA angle / RMSD | Ordered backbone angle / RMSD |
|---|---:|---:|---:|
| A / B | 119.4435° / 0.7385 Å | 119.4213° / 0.7183 Å | 120.1683° / 0.1237 Å |
| A / C | 120.2295° / 1.3270 Å | 120.1895° / 1.3384 Å | 120.1128° / 0.1218 Å |
| B / C | 120.2792° / 1.0302 Å | 120.3436° / 1.0701 Å | 119.7211° / 0.1305 Å |

The default protocol comparison requires at least 60 mapped residues and 60% of the observed union. For valid routes it calls a comparison sensitive when any preset pair changes by more than 0.5° or 0.15 Å. All common backbone / common CA differences fall within those bounds. The largest changes are 0.06439° and 0.03988 Å. The ordered selection covers 29.88% of the observed union. Its lower RMSDs describe that subset, and the three-protocol comparison is insufficient under the frozen criteria. [Measured Jobs and independent checks](../../../../acceptance/runs/acc-1-coordinate-method/clean.json)

The three pairwise results reuse the same chains. B factors reflect both disorder and refinement. Any claim about a common symmetry axis, solution stoichiometry, binding or biological activity would require additional measurements. [Frozen interpretation and limits](data/criteria.json)

## Reproduction

From the package root, run the command below with an interpreter containing the two pinned review dependencies. The checker copies the GX runtime and inputs into a fresh temporary directory, starts four detached CPU Jobs, reopens their database, checks independent results and verifies recovery of the original Job IDs. Its one-degree perturbation and tighter cutoff are explicitly labeled controls. [Review dependency pins](requirements-review.txt) · [Checker](../../../../acceptance/acc1_coordinate_method.py)

```sh
python3 -B acceptance/acc1_coordinate_method.py --output /tmp/tl1a-coordinate-check.json
```

From this adapter directory the same calculations can be run individually. The review script’s default output, when given only the structure and criteria, contains the numerical reference for all protocols.

```sh
python3 -I -B worker.py quality --structure data/2RE9.cif --out /tmp/tl1a-quality
python3 -I -B worker.py geometry --structure data/2RE9.cif --criteria data/criteria.json --quality /tmp/tl1a-quality/quality.json --protocol common_backbone --out /tmp/tl1a-fit
python3 -I -B review.py --structure data/2RE9.cif --criteria data/criteria.json --observation /tmp/tl1a-fit/geometry.json --out /tmp/tl1a-independent.json
```

The atom correspondence and proper pair-fit definitions follow the previously qualified `structure-geometry@1.0.0` method, which adapted Chaperone's `SymmetryMetricsBlock` at commit `2d3ac1e7ce8078e7b99ce1511940fda61c7198ae`. This package implements the fit independently in the standard library. The coordinate archive is distributed under CC0-1.0. Upstream Chaperone supplied an MIT notice. [PDB archive terms](https://www.wwpdb.org/about/usage-policies) · [CC0 text](data/LICENSE.cc0) · [Chaperone notice](LICENSE.chaperone)
