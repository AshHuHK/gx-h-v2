# Historical migration fixture

`gx-23d581c.bundle` contains the original two-commit GX history through `23d581c27f46e65324246dff269b6ce77c9b3e9a`. Its tree is `61c9bbc5e9dcf6c3f017cec42809b55cb633e2fa`. Both migration tests open this local bundle. The rollback test installs the currently tested release files at the same path, opens the old state, and restores the original installation and its SQLite WAL-consistent backup.

Inspect it from any release extraction.

```bash
git bundle list-heads gx-h/tests/fixtures/gx-23d581c.bundle
sha256sum gx-h/tests/fixtures/gx-23d581c.bundle
(cd gx-h && python3 -B -m unittest -v tests.test_v2_fencing.BaselineMigration tests.test_v2_rollback)
```

The SHA-256 is `0dd996d24c3c303c385b4d44869168b3f123d086a74adcf2c607aef669f02037`. To recreate the fixture from a repository containing the original object, set `original_gx` to its absolute directory and run the following from the release root.

```bash
fixture_build=$(mktemp -d)
git init --bare --quiet "$fixture_build"
git -C "$fixture_build" fetch --quiet --no-tags "$original_gx" \
  23d581c27f46e65324246dff269b6ce77c9b3e9a:refs/heads/migration-baseline
git -C "$fixture_build" -c pack.threads=1 bundle create --version=2 \
  "$PWD/gx-h/tests/fixtures/gx-23d581c.bundle" refs/heads/migration-baseline
```
