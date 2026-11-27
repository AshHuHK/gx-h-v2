# BglB published-panel retrospective

Does an archived conservation score rank the catalytic efficiencies recorded for held-out sites of *Paenibacillus polymyxa* BglB? The outcome is the within-study pNPG hydrolysis efficiency in S1 Table column 6. A retrospective score uses the negative percentage in S3 Table column 3. Lower percentages receive higher predicted ranks. All rows come from this one study. Carlin et al. published their article and supplements under CC BY 4.0 in 2016. This release includes the original files and introduces the retrospective analysis code. [Article](https://doi.org/10.1371/journal.pone.0147596) · [Licence](https://creativecommons.org/licenses/by/4.0/)

| Local original | Use | SHA-256 |
| --- | --- | --- |
| [article.xml](data/article.xml) | Published methods, numbers in the main text and licence | 49237dd4375cbaaa56a893f5acf7f68a0645cbf4df1f1780e1b34fa7deb8fc76 |
| [S1-table.docx](data/S1-table.docx) | Experimental observations and reported uncertainty | b580e7573ece03bf1dba2c0dc27238a12b1bf627eb1307f35a0cedffbc1b2a73 |
| [S3-table.docx](data/S3-table.docx) | Published conservation percentages and original variant IDs | d08605c3f5370061b1cdfe5119e215e45338b4ff173f0c56ee244af964bdf0e9 |
| [S1-text.docx](data/S1-text.docx) | Measurement conditions and technical replication | 3b5cab3fdd1d3a4242add86ad6bc1437d644838d10f4f63a1735cbe42d2f864f |

Acquired 2026-11-27 from the publisher. The Goal imports the two original tables with separate GX material IDs and acquisition descriptors. The protocol gives pH 7.5, a 420 nm readout and triplicate assays. The article reports ten biological WT replicates. The table does not identify each variant’s biological replicates or provide a per-row assay temperature. S1 Text does not supply a separate assay temperature. [S1 Table](https://doi.org/10.1371/journal.pone.0147596.s006) · [S3 Table](https://doi.org/10.1371/journal.pone.0147596.s008) · [S1 Text](https://doi.org/10.1371/journal.pone.0147596.s009)

## Run

From the release root with the existing GX deployment:

```sh
bin/gx-p0 research --state /path/to/study-state --run-key bglb-panel-v1 \
  --goal-file examples/bglb-retrospective/question.md \
  --goal-config examples/bglb-retrospective/goal.json
```

The run grants one CPU method. Supply both original artifact references to one node. Registration binds them by the GX-verified hashes regardless of order. The method emits compact numerical readouts in `summary.json` and the original table row numbers, statuses, uncertainties, site assignments and scores in `records.json`. Repeat the command with the same state and key to continue the study. For the local regression and an actual GX CPU Job with scripted Lead transport:

```sh
PYTHONPATH=gx-h .venv/bin/python -m unittest -v acceptance.science_capabilities.test_bglb_retrospective
```

## Interpreting the comparison

The fixed site split is SHA-256 of `GX-BglB-S1-site-v1:<position>`, interpreted as a big-endian unsigned integer. Remainder zero modulo three selects holdout sites. Site membership has no dependence on measurements. S3 column 1 and its published outcome-sorted order never enter scoring. This is a repeatable retrospective split of public data, and the original tables were inspected during implementation. The method fits no coefficients.

S1 records 78 quantified efficiencies, 8 left-censored efficiencies and 14 variants without detectable soluble yield. The main text instead describes 89 solubly purified variants of which 6 fell below the kinetic detection limit. S1 assigns WT 176,000 ± 8,000 M⁻¹ min⁻¹, while the article reports 171,000 ± 8,000. Preserve both descriptions and use S1 as the basis for row-level comparisons. Unfitted individual constants and blank cells retain their source state. A low yield or an undetectable reaction supplies no numerical zero.

The fixed holdout contains 41 variants. Thirty have quantified catalytic efficiency and 14 of these also have an archived conservation score. The tie-adjusted Spearman coefficient for those 14 pairs is 0.260. WT gives every variant the same predicted activity, hence its rank correlation is undefined. Its median absolute log₁₀ error on the same 14 pairs is 0.388. These observations support reporting a weak, subset-specific association. Coverage and the single study limit the interpretation of other published records. The comparison does not establish generalization.
