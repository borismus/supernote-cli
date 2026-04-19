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

supernote source ls [--days-ago N] [--limit N] [--json]  # list source docs that have digests

supernote digest ls [--limit N] [--json]              # list digest records
supernote digest <id>[,<id>...] \                     # print content AND render the
         [-o PATH] [--no-annotation] [--force] [--json]  # handwritten annotation as PNG

supernote note ls [--days-ago N] [--limit N] [--json] # list .note files under /Note/
supernote note <file-id> \                            # download + render + Ollama-OCR
         [-o DIR] [--model M] [--force] [--json]        # a cloud .note by id
```

`supernote digest <id>` prints the auto-transcribed content and renders the handwritten annotation (the note you drew on top of the highlighted passage) as PNG. `-o` can be either a directory (files named `{digest_id}.png`, default: CWD) or a `.png` path used as the target filename directly (single ID only). Multi-page annotations produce `{digest_id}_p1.png`, `{digest_id}_p2.png`, etc. (or `{stem}_p{N}.png` siblings in file-path mode). Existing PNGs are skipped unless `--force`. Digests without an annotation print `no annotation for {id}` to stderr and write no file. Use `--no-annotation` to print content only.

`supernote source ls` groups digests by their source document (e.g. `/Document/MyBook.pdf`) and prints `{digest_count}  {latest_modified}  {source_path}`, most-recent first.

`supernote note ls` lists `.note` files under `/Note/` (recursive), most-recent first, with `{size}  {update_time}  {id}  {folder_path}/{file_name}`.

`supernote note <file-id>` downloads the cloud `.note` to a tempfile, renders each page to `page_{N}.png` under `-o` (default `./note-{id}/`), and runs Ollama vision OCR on each (default model `qwen3-vl:8b`). Requires Ollama running locally; if unreachable, `ocr_text` comes back empty but page rendering still completes. Use `--json` for a structured per-page array.

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

- v0.1: cloud API (login, ls, download, sync, source/digest listing, annotation rendering).
- `.note` OCR: `list_notes`, `render_note`, `extract_note_text`, `ocr_note` (local file), `ocr_note_from_cloud` (by file id), `ocr_image` in `supernote_cli.api` / `supernote_cli.ocr`; plus `supernote note ls` and `supernote note <file-id>` CLI verbs. Requires Ollama running locally with a vision model pulled (default `qwen3-vl:8b`).

## Tests

Unit tests are offline:

```
uv run pytest tests/test_auth_unit.py
```

Live smoke tests hit the real API and require `.env` plus the gate:

```
SUPERNOTE_LIVE_TEST=1 uv run pytest tests/test_smoke_live.py -v
```
