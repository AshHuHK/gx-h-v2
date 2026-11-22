# V3 Phase Gates

1. Preserve v2.1 branch and frozen product commit.
2. Inspect actual HEAD; preserve later commits/local changes.
3. Gate 0: 2-hour real-model multi-Root baseline before hook/hot-path changes.
4. P0: lean loader, digest, deterministic resolver, versioned import namespace, register(ctx).
5. P1: migrate science_grid@2 with existing Capability Manifests and Tools.
6. P2: independent plugin proof (Tool + existing-format Capability or Evaluator).
7. P3: Run composition and same-process multi-version isolation.
8. One phase per commit with focused tests, full regression, report and rollback.
9. prepare_dispatch reorder only after Gate 0 and with before/after measurements.
10. v2.1 remains stable fallback; v3 is opt-in until P3 cold-resume/rollback pass.
