# supernote-cli

CLI and Python client for the Supernote cloud API (`viewer.supernote.com`).

## Install

```
uv tool install git+https://github.com/bsmus/supernote-cli  # once published
# or from a local checkout:
cd supernote-cli && uv sync
```

## Credentials

Put your account credentials in a `.env` file. `supernote-cli` never stores your password — only the session token it receives from the API.

```
SUPERNOTE_USER=you@example.com
SUPERNOTE_PASSWORD=your-password
# Optional:
# SUPERNOTE_EQUIPMENT_NO=MACOS_<uuid>
```

`.env` is discovered from the current directory walking upwards (standard python-dotenv behavior). The session token is cached at `$XDG_CONFIG_HOME/supernote-cli/token.json` (fallback `~/.config/supernote-cli/token.json`), chmod 0600.

## CLI

```
supernote login                                       # writes token cache
supernote logout                                      # deletes token cache
supernote whoami                                      # show cached account + token age

supernote ls [PATH] [--json]                          # list folder contents
supernote download <file-id> [-o PATH]                # download a single note file
supernote sync <path> --out DIR \
         [--days-ago N] [--dry-run] [--recursive]

supernote digest ls [--limit N] [--json]              # list digest records
supernote digest <id>[,<id>...] \                     # print content AND render the
         [-o DIR] [--no-annotation] [--force] [--json]   # handwritten annotation as PNG
```

`supernote digest <id>` prints the auto-transcribed content and renders the handwritten annotation (the note you drew on top of the highlighted passage) to `{out_dir}/{digest_id}.png`. Multi-page annotations produce `{digest_id}_p1.png`, `{digest_id}_p2.png`, etc. Existing PNGs are skipped unless `--force`. Digests without an annotation print `no annotation for {id}` to stderr and write no file. Use `--no-annotation` to print content only.

Global flags: `--no-cache`, `--verbose`, `--equipment-no`.

## Library

```python
from supernote_cli import Client, api

c = Client.from_env()             # loads .env + cached token

dir_id, contents = api.resolve_path(c, "Note")
for note in contents:
    if not note.is_folder:
        print(note.file_name, note.size)

digests = api.fetch_digests_by_ids(c, [h.id for h in api.fetch_digest_hashes(c, size=5)])

# Render the handwritten annotation for a digest to PNG
for d in digests:
    paths = api.render_handwriting(c, d, out_dir="/tmp/hw")
    if paths:
        print(d.id, "->", paths)
```

`Client` handles auth transparently: an expired token triggers a re-login if `.env` credentials are available. Rendering uses `supernotelib` + `pillow` (main deps).

## Status

- v0.1: cloud API fetching (login, ls, download, sync, digest listing, annotation rendering).
- v0.2 (planned): OCR of the rendered handwriting via local vision models.

## Tests

Unit tests are offline:

```
uv run pytest tests/test_auth_unit.py
```

Live smoke tests hit the real API and require `.env` plus the gate:

```
SUPERNOTE_LIVE_TEST=1 uv run pytest tests/test_smoke_live.py -v
```
