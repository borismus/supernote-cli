# Publishing to PyPI

Not yet published. When ready, follow the steps below.

## One-time account setup

1. Register at [pypi.org](https://pypi.org/account/register/) and [test.pypi.org](https://test.pypi.org/account/register/) (same email, distinct passwords fine).
2. Enable 2FA on both (required since 2024).
3. Create API tokens:
   - TestPyPI: [test.pypi.org/manage/account/token](https://test.pypi.org/manage/account/token/) — scope "Entire account" for the first upload.
   - PyPI: [pypi.org/manage/account/token](https://pypi.org/manage/account/token/) — same.
4. After the first successful upload on each, regenerate the token scoped to the `supernote-cli` project and save in a password manager.

## Pre-flight

`pyproject.toml` already has `name`, `version`, `readme`, `license`, `urls`, `classifiers`, and `keywords`. A `LICENSE` file is present at the repo root. Before each release:

- Bump `version = "x.y.z"` in `pyproject.toml` (PyPI refuses re-uploads of the same version).
- Make sure `README.md` renders — visit the [markdown preview](https://pypi.org/project/supernote-cli/) after first publish; fix anything mangled.
- Run offline tests: `uv run pytest tests/test_auth_unit.py`.
- Optional: live tests with `SUPERNOTE_LIVE_TEST=1 uv run pytest tests/test_smoke_live.py`.
- Tag the release: `git tag vX.Y.Z && git push --tags`.

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
uv publish \
  --publish-url https://test.pypi.org/legacy/ \
  --token <testpypi_token>
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
uv publish --token <pypi_token>
```

Verify from scratch:

```
uv tool install supernote-cli
supernote --help
```

## Subsequent releases

1. Bump `version` in `pyproject.toml`.
2. Tag: `git tag vX.Y.Z && git push --tags`.
3. `rm -rf dist/ && uv build && uv publish`.

## Gotchas

- `uv publish` silently no-ops if `dist/` is empty — always `uv build` first.
- PyPI refuses re-uploads of an existing version. If you hit a problem post-upload, bump to a new version; you can't patch in place.
- Token hygiene: store as `UV_PUBLISH_TOKEN` env var once you have project-scoped tokens, so the command line drops `--token`.
- Don't commit tokens. Don't include them in any script in the repo.
