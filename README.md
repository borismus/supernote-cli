# supernote-cli

CLI and Python client for the Supernote cloud API (`viewer.supernote.com`).

## Install

`supernote-cli` depends on `supernotelib`, which pulls in `pycairo` for note rendering. On macOS, that means you need the native Cairo toolchain installed externally before `uv` can build the Python package:

```bash
brew install pkg-config cairo
```

Then install the project:

```
uv tool install git+https://github.com/borismus/supernote-cli  # once published
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
supernote login | logout | whoami

supernote ls [PATH] [--json]                          # list folder contents
supernote download <path> [--by-id ID] [-o PATH]      # download by remote path (or id)
supernote upload <local> <remote-dir> [--overwrite]   # upload a local file
supernote delete <path>... [--by-id ID]               # delete remote file(s)
supernote sync <path> -o DIR \
         [--days-ago N] [--dry-run] [--recursive]

supernote source ls [--days-ago N] [--limit N] [--json]

supernote digest ls [--limit N] [--days-ago N] [--json]
supernote digest <id>[,<id>...] \                     # JSON with digest/annotation/handwritten_image
         [-o DIR] [--no-ocr] [--model M] [--force]

supernote note ls [--days-ago N] [--limit N] [--json]
supernote note <file-id> \                            # JSON per-page array; renders + Ollama OCR
         [-o DIR] [--no-ocr] [--model M] [--force]
```

Global flags: `--no-cache`, `--verbose`, `--equipment-no`.

### Addressing

Most commands take a remote path (`Note/Inbox/foo.note`). `download` and `delete` also accept `--by-id <ID>` as an escape hatch. `upload` expects the destination folder to already exist — it won't create missing folders. `delete` removes the remote file immediately with no confirmation prompt; `upload --overwrite` uses it internally and waits for the deletion to propagate server-side before re-uploading.

### `digest <id>` — JSON output

`digest <id>` prints a JSON record using Supernote's own terminology:

```json
{
  "id": "832783687476051968",
  "digest": "just as we've become a culture of overeaters...",
  "annotation": "completely correlated",
  "handwritten_image": "attachments/832783687476051968.png",
  "source_path": "/Document/Breath.epub",
  "last_modified": "2026-04-18T11:42:00"
}
```

- `digest` — the highlighted passage (Supernote's device transcription).
- `annotation` — Ollama OCR of your handwritten note drawn on top. `null` when `--no-ocr` or there's no annotation.
- `handwritten_image` — path to the rendered PNG, relative to `-o` (default CWD). `null` when no annotation. Array for multi-page.
- Multiple comma-separated IDs produce a JSON array.

OCR runs by default; pass `--no-ocr` to skip it (no Ollama round trip). With OCR on, Ollama must be reachable — if not, the command exits 2 with a clear error.

### `note <id>` — JSON output

`note <id>` downloads the cloud `.note` to a tempfile, renders each page to PNG under `{-o}/attachments/`, and runs Ollama OCR per page. Output is a JSON array:

```json
[
  {
    "page": 1,
    "transcript": "device OCR text from supernotelib",
    "annotation": "Ollama OCR text",
    "handwritten_image": "attachments/1138647043762290688-p1.png"
  }
]
```

Default `-o` is `./note-{id}/`. `transcript` is the on-device OCR (may be empty); `annotation` is Ollama's read of the PNG.

### Ollama

Default model is `qwen3-vl:8b`; change with `--model`. If Ollama returns an error mid-run (e.g. model not pulled), the CLI surfaces the error to stderr once and emits `annotation: null` for remaining items — partial results still print. Use `OLLAMA_HOST` to point at a non-default daemon.

## Library

```python
from supernote_cli import Client, api

c = Client.from_env()             # loads .env + cached token

# List .note files under /Note/ (recursive)
for folder_path, note in api.list_notes(c):
    print(note.id, f"{folder_path}/{note.file_name}")

# Group digests by source document (PDF/EPUB) and get full Digest records
for src in api.list_digested_sources(c, days_ago=30):
    print(src.source_stem, len(src.digests))
    # Render each digest's handwritten annotation to PNG
    for d in src.digests:
        paths = api.render_handwriting(c, d, out="/tmp/hw")
        if paths:
            print(d.id, "->", paths)

# Upload a local PDF to an existing remote folder, then delete it
note = api.upload_file(c, "~/book.pdf", "Document/Books/")
print(note.id, note.file_name)
api.delete_file(c, note)

# Download + render + Ollama-OCR a .note by cloud id
pages = api.ocr_note_from_cloud(c, "1138647043762290688", "/tmp/wh")
for p in pages:
    print(p.index, p.ocr_text)
```

`Client` handles auth transparently: an expired token triggers a re-login if `.env` credentials are available. Rendering uses `supernotelib` + `pillow` (main deps); OCR talks to a local Ollama daemon.

## Status

- v0.2 (breaking): standardized `-o/--output` across commands, path-based `download` / `delete` (with `--by-id` fallback), JSON-always output for `digest <id>` / `note <id>` using Supernote terms (`digest` / `annotation` / `handwritten_image`), OCR default on with hard-fail on Ollama unreachable, new `upload` and `delete` verbs.
- `.note` OCR: `list_notes`, `render_note`, `extract_note_text`, `ocr_note` (local file), `ocr_note_from_cloud` (by file id), `ocr_image` in `supernote_cli.api` / `supernote_cli.ocr`.
- Upload: `api.upload_file(client, local_path, remote_dir, overwrite=False)` and `supernote upload` CLI. Implements Supernote's `file/upload/apply` → signed S3 PUT → `file/upload/finish` flow; `remote_dir` must already exist (no auto-mkdir).
- Not yet on PyPI. Install via `uv tool install git+https://github.com/borismus/supernote-cli` or add a local path dep (`{ path = "…", editable = true }`). Planned to publish after living with the API for a bit — see the publish playbook in [docs/publishing.md](docs/publishing.md).

## Tests

Unit tests are offline:

```
uv run pytest tests/test_auth_unit.py
```

Live smoke tests hit the real API and require `.env` plus the gate:

```
SUPERNOTE_LIVE_TEST=1 uv run pytest tests/test_smoke_live.py -v
```
