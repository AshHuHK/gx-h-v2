# Protein Goal provider

The released `org.gx.protein@0.1.0-v6.1` retains its original digest `ac5b5d5cf96e942bfc57dc8492aa56b77ad9ecee0716735a3e6338463630fce9`. An existing Goal keeps that Run's original package and capability pins. V6.1 resolves its Goal callbacks through an exact compatibility mapping. Both ends of that mapping are checked. New Goals can explicitly request `org.gx.protein@0.2.0` and the subset of method pins they need. The installed composition grants a geometry re-evaluation tool only when its geometry capability is selected. [Compatibility mapping](../../../gx-h/config/goal_provider_compatibility.json)

The new package registers three Goal adapters for the released coordinate inventory, ordered atom selection, and independent SVD review. The original Job programs, capability content pins and 2RE9 input hash remain the scientific basis of these methods. The retrospective comparison uses the release's frozen threshold and comparison formulas, carries its evaluator source digest, and attaches the original Job output references. The package also contributes a separate deposited-coordinate method with two eligible inputs, 2RE9 and 2O0O, for comparative research. [Installed package](../../../gx-h/plugins/installed/org.gx.protein/0.2.0/plugin.py)

From the checkout, run with the locked CPython environment and its Biopython and NumPy wheels. The tests exercise actual Job processes, scoped tool denial and cold re-opening of pending and completed work.

```sh
(cd gx-h && "$GX_PYTHON" -B -m unittest -v tests.test_v6_protein tests.test_v61_protein_provider tests.test_v6_operator_recovery)
```
