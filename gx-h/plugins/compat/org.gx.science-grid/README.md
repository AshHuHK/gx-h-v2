# Native delivery for the released science-grid workflows

The installed workflow catalogue selects `grid_native.WORKER` for `science_grid@3`. It handles the worker's frozen input packet, tool settings, planner product schema, evidence projection and repair limit. The existing TeamService validates the pinned workflow's product; JobService and the Driver retain dispatch, settlement and recovery.

The previous release shipped `science_inputs.py` and `science_refresh_v2.py` under `gx_core.p0`. Published grid and geometry graph digests include the original Python module names and file SHA-256. The release-owned compatibility directory contributes that import path. Both files retain their published bytes. The old `org.gx.science-grid@2.0.0` and `@3.0.0` plugin directories remain available with their original content digests. External plugins use the registration API and installed package catalogue.
