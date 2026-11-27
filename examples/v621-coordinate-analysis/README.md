# Source receipts for deposited coordinates

The two descriptors bind the operator's source records to the published mmCIF revisions of 2RE9 and 2O0O. Their publication DOI is shared. The available records leave acquisition details and biological sampling independence unknown. Data and version identifiers come from the [archive provenance](../../acceptance/v61/adaptive/data/provenance.json).

For each original, take its exact artifact reference and SHA-256 from the new Goal's materials. Register it before any Job consuming the material is admitted.

```bash
bin/gx-p0 observe --state "$study" --run-id "$run_id" --source wwPDB-2RE9-revision-1.4 \
  --artifact-ref "$source_ref" --sha256 "$source_sha" \
  --acquisition examples/v621-coordinate-analysis/2RE9.json
```

The new provider is `org.gx.protein@0.3.0`. Its `deposited-chain-geometry@2` capability computes a coordinate inventory and pair fits under the declared atom selection. The sole source role is `structure`. The `geometry.json` output is an analysis. The measured numerator and denominator refer to residue positions in the deposited asymmetric unit. Scientific inputs are versioned by content, including inputs acquired after the opening plan. A derived control must identify its parent pins in `derived_from` and its transformation in `processing`. It retains its parent's original observations.

Older coordinate quality, geometry, review and deposited-chain invocations remain available through their original provider versions. Their output declarations and historical Job manifests retain their original semantics.
