# Publishing to PyPI

Step-by-step playbook for cutting a release. First release was v0.3.0.

## One-time account setup

1. Register at [pypi.org](https://pypi.org/account/register/) and [test.pypi.org](https://test.pypi.org/account/register/) (same email, distinct passwords fine).
2. Enable 2FA on both (required since 2024).
3. Create API tokens:
   - TestPyPI: [test.pypi.org/manage/account/token](https://test.pypi.org/manage/account/token/) — scope "Entire account" for the first upload.
   - PyPI: [pypi.org/manage/account/token](https://pypi.org/manage/account/token/) — same.
4. After the first successful upload on each, regenerate the token scoped to the `supernote-cli` project and save in a password manager.
5. Drop the tokens into `~/.pypirc` so `twine` picks them up automatically (this repo uploads via twine — see Gotchas for why):

   ```ini
   [distutils]
   index-servers =
       pypi
       testpypi

   [pypi]
   username = __token__
   password = pypi-AgEI...your-token...

   [testpypi]
   repository = https://test.pypi.org/legacy/
   username = __token__
   password = pypi-AgEN...your-testpypi-token...
   ```

   `username` must be literally `__token__` (two underscores either side). `chmod 600 ~/.pypirc` since it holds secrets.

## Pre-flight

`pyproject.toml` already has `name`, `version`, `readme`, `license`, `urls`, `classifiers`, and `keywords`. A `LICENSE` file is present at the repo root. Before each release:

- Bump `version = "x.y.z"` in `pyproject.toml` (PyPI refuses re-uploads of the same version). Run `uv sync` afterward so `uv.lock` matches.
- Make sure `README.md` renders — visit the [PyPI page](https://pypi.org/project/supernote-cli/) after first publish; fix anything mangled. The README is baked into the wheel's METADATA, so any edits require a rebuild.
- Run offline tests: `uv run --extra dev pytest tests/test_auth_unit.py`.
- Optional: live tests with `SUPERNOTE_LIVE_TEST=1 uv run --extra dev pytest tests/test_smoke_live.py`.
- Tag the release: `git tag vX.Y.Z && git push origin vX.Y.Z` (push the specific tag — avoid `--tags`, which pushes everything local).

## Build

```
rm -rf dist/
uv build
```

Produces `dist/supernote_cli-X.Y.Z.tar.gz` (sdist) and `dist/supernote_cli-X.Y.Z-py3-none-any.whl` (wheel). Inspect the wheel's metadata if you want to sanity-check:

```
unzip -p dist/supernote_cli-*.whl '*/METADATA' | head -30
```

## Upload to TestPyPI first

```
uv tool run --from twine twine upload --repository testpypi dist/*
```

Validate in a throwaway env (TestPyPI doesn't mirror real deps, so we need `--extra-index-url`):

```
uv run --with supernote-cli \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  -- supernote --help
```

## Upload to real PyPI

```
uv tool run --from twine twine upload dist/*
```

Verify from scratch (give the index ~1-2 min to catch up):

```
uv tool install --upgrade supernote-cli
supernote --help
```

## Subsequent releases

1. Bump `version` in `pyproject.toml`; run `uv sync`.
2. Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. `rm -rf dist/ && uv build && uv tool run --from twine twine upload dist/*`.

## Gotchas

- **Use twine, not `uv publish`.** `twine` reads `~/.pypirc` automatically. `uv publish` does NOT — it expects `--token`, `UV_PUBLISH_TOKEN`, or OIDC trusted publishing (CI-only). That's why this playbook uses twine.
- Always `rm -rf dist/` before `uv build`. Otherwise stale artifacts from a previous version get re-uploaded alongside the new ones.
- PyPI refuses re-uploads of an existing version. If you hit a problem post-upload, bump to a new version; you can't patch in place.
- Don't commit tokens. Don't include them in any script in the repo. `chmod 600 ~/.pypirc`.
