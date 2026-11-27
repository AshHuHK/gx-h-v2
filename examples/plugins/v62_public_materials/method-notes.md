# Public reading fixture GX-MASK-NOTE-1

## Scope of the comparison

The registered deposited-chain-geometry@1 method pairs identical label positions, residue names and atom names in model 1, entity 1, across the three chains of each deposited asymmetric unit. It fits the shared atoms within each deposition. A comparison across the two depositions therefore compares within-deposition summaries. Its method card names PDB 2RE9 and PDB 2O0O as the supported structures.

The common_backbone mask requires shared N, CA, C and O atoms at each position. The ordered_backbone mask adds full occupancy and a displacement parameter of at most 40 square angstroms for every selected atom on each of the three chains. Thus the ordered mask can retain all or only a subset of the common positions. Each count can be related to the same observed-union denominator within one deposition.

## An alternative explanation

Changing the atom mask can change both the residues retained and the estimated geometry. A difference in fitted RMSD can arise because two fits used different subsets. The relevant comparison reports the selected residue count against the observed-union denominator for each entry and mask. A resolution difference alone does not quantify atom coverage or predict the direction of the RMSD difference.

## Sources

The source for this local methods note is org.gx.protein@0.2.0, methods/deposited_geometry.json, /manifest/method_card, SHA-256 74db8d27a3e07e48c6e61ac7b3ef3f1508fe2eb50c4878d8a0a4db010bd39f88. The archived deposition citation is Jin et al., 2007, DOI 10.1016/j.bbrc.2007.09.097. The note describes a way to distinguish hypotheses. It contains no new measurements.
