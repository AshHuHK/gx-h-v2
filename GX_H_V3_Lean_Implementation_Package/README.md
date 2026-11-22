# GX-H v3 Lean Implementation Package

Contents:
- GX_H_V3_BLUEPRINT.md: unified lean architecture and P0–P3.
- IMPLEMENTATION_PROMPT.md: Coding Agent prompt.
- contracts/plugin-manifest.schema.json: reference manifest schema.
- contracts/author_api_reference.py: reference API; derive final API from science_grid@2.
- examples/hello_research_plugin/: register(ctx) and relative-import example.
- acceptance/V3_ACCEPTANCE_MATRIX.md: release gates.
- architecture/V3_PHASE_GATES.md: sequencing.

Baseline anchors: reviewed v2.1 HEAD 61ed0067448658e02d181b3cb85d7f68e39b6616; frozen product code a05dcef64522fbbbc12120b1d78da518e6886b10; Codex 0.169.0. These are not reset instructions; inspect actual HEAD.

Trust note: static import lint is not a security boundary. In-process Python is trusted code, not a sandbox. Untrusted/expensive work uses existing isolated GX Job/Capability Runtime.

This is a design handoff, not evidence v3 is implemented.
