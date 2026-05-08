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
supernote digest <id>[,<id>...] \                     # blockquote to stdout; nothing written
         [-o PATH] [--ocr {supernote,ollama}] [--model M] [--force] [--json] [--prompt TEXT]

supernote note ls [--days-ago N] [--limit N] [--json]
supernote note <name|path|id> \                       # device transcript to stdout; nothing written
         [-o DIR] [--ocr {supernote,ollama}] [--model M] [--force] [--json] [--prompt TEXT]
```

Global flags: `--no-cache`, `--verbose`, `--equipment-no`.

### Addressing

Most commands take a remote path (`Note/Inbox/foo.note`). `download` and `delete` also accept `--by-id <ID>` as an escape hatch. `upload` expects the destination folder to already exist — it won't create missing folders. `delete` removes the remote file immediately with no confirmation prompt; `upload --overwrite` uses it internally and waits for the deletion to propagate server-side before re-uploading.

### `digest <id>` — blockquote to stdout; `-o` for the PNG, `--ocr ollama` for LLM transcription

By default, `digest <id>` prints just the highlighted passage. Nothing
is written to disk:

```
$ supernote digest 832783777540341760
> I've decided to throw my chemoreceptors into the ring...
```

- The `>` block is the highlighted passage Supernote already transcribed (`digest.content`).
- If the digest also has a handwritten annotation, a one-line note is printed to **stderr**: `note: digest <id> has untranscribed handwriting; pass --ocr ollama to transcribe (or -o PATH to save the PNG)`. This keeps stdout clean for piping while making sure you know there's more to pull.

Pass `-o PATH` to also persist the rendered handwriting PNG. `PATH` ending in `.png` is treated as a file path; otherwise it's a directory:

```
$ supernote digest 832783777540341760 -o ann.png
> I've decided to throw my chemoreceptors into the ring...

_(no transcript)_

![](ann.png)
$ ls
ann.png
```

```
$ supernote digest 832783777540341760 -o annotations/
> I've decided to throw my chemoreceptors into the ring...

_(no transcript)_

![](annotations/832783777540341760.png)
```

Multi-page digests fan out as `{stem}_p1.png`, `{stem}_p2.png`, ... in file mode and `{digest_id}_p1.png`, `{digest_id}_p2.png`, ... in dir mode.

Pass `--ocr ollama` to transcribe the handwriting via local Ollama vision OCR. With `-o`, the OCR text replaces the placeholder and a sibling `content.md` (file mode: `{stem}.md`; dir mode: `content.md`) is written as a cache marker:

```
$ supernote digest 832783777540341760 --ocr ollama -o ann.png
> I've decided to throw my chemoreceptors into the ring...

could this be something for dad to look into?

![](ann.png)
```

Without `-o`, `--ocr ollama` still works — the PNG renders to a tempdir, gets OCR'd, and is discarded. Stdout is just `blockquote + ocr text`.

Pass `--json` for the structured shape (Supernote's own terms):

```json
{
  "id": "832783687476051968",
  "digest": "just as we've become a culture of overeaters...",
  "annotation": "completely correlated",
  "handwritten_image": "./832783687476051968.png",
  "source_path": "/Document/Breath.epub",
  "last_modified": "2026-04-18T11:42:00"
}
```

`handwritten_image` is `null` unless `-o` was passed. `annotation` is `null` unless `--ocr ollama` was passed. With multi-page handwriting `handwritten_image` is an array.

Multiple comma-separated IDs print one markdown block per digest (separated by a blank line) or a JSON array. `-o file.png` requires a single id; `-o dir/` allows multiple ids when `--ocr ollama` is off.

### `note <name|path|id>` — device transcript to stdout; `-o` for page PNGs, `--ocr ollama` for LLM transcription

The note target is resolved in this order:

- **All-digits** → numeric file id.
- **Contains `/`** → full path under `Note/` (or absolute `Note/sub/foo.note`).
- **Otherwise** → basename (with or without `.note` suffix). If multiple notes share that basename across folders, the resolver errors with the matching paths so you can disambiguate.

`note ls` now emits `mtime  name`, sorted oldest-first / newest-last (so the latest note is right above your prompt). Names are shown without the `.note` suffix; the resolver re-appends it transparently. Use `--json` for the full record (id, folder_path, file_name, size, update_time).

```
$ supernote note ls --limit 5
2026-04-24 08:11  San Francisco Note, April 20
2026-04-27 07:36  20260424_081053
2026-05-01 07:39  20260429_132435
2026-05-05 21:10  Eliana 2
2026-05-05 21:21  20260501_073927
$ supernote note 20260501_073927
## Page 1

(device-OCR transcript for page 1, written by the tablet)

## Page 2

...
```

- Pages where device OCR was off (or produced no recognizable text) render as `_(no transcript)_`.
- No PNGs are rendered or persisted in this default path — only the .note file is downloaded.

Pass `-o DIR` to also render and persist `page_N.png` into `DIR`. The markdown then includes per-page `![](DIR/page_N.png)` refs.

Pass `--ocr ollama` to run local Ollama vision OCR per page instead of the device transcript (higher quality, slower, requires Ollama). With `-o`, `content.md` is written into the dir as a cache marker; without `-o`, PNGs render to a tempdir and are discarded after OCR.

Pass `--json` for the v0.2 per-page structured array:

```json
[
  {
    "page": 1,
    "transcript": "device OCR text from supernotelib",
    "annotation": "Ollama OCR text (null without --ocr ollama)",
    "handwritten_image": "MyNotebook/page_1.png"
  }
]
```

### `--ocr` engines

Both `digest <id>` and `note <id>` accept `--ocr {supernote,ollama}`:

| Engine | `note` behavior | `digest` behavior |
|---|---|---|
| `supernote` (default) | per-page device transcript from supernotelib (`extract_note_text`); empty pages render `_(no transcript)_` | no annotation transcription (Supernote's device OCR doesn't cover digest handwriting); without `-o` a stderr hint is printed when handwriting exists; with `-o` the body shows `_(no transcript)_` next to the image ref |
| `ollama` | per-page Ollama vision OCR; replaces device transcript | per-page Ollama vision OCR of the rendered handwriting PNG(s) |

### Custom OCR prompt

`--prompt TEXT` (only meaningful with `--ocr ollama`) layers project-specific transcription rules on top of the default OCR prompt. Useful for preserving inline markers verbatim:

```
$ supernote note <id> --ocr ollama --prompt "When a line begins with → or ☐, transcribe it verbatim including the leading symbol; preserve multi-line continuation."
```

The text is appended under an `Additional instructions:` section after the default OCR prompt. **The `content.md` cache does not track the prompt.** If you change the prompt and want fresh output, pass `--force` to invalidate.

### Ollama

Default model is `qwen3-vl:8b`; change with `--model`. If Ollama returns an error mid-run (e.g. model not pulled), the CLI surfaces the error to stderr once and emits `annotation: null` for remaining items — partial results still print. Use `OLLAMA_HOST` to point at a non-default daemon.

## Library

```python
from supernote_cli import Client, api

c = Client.from_env()             # loads .env + cached token

# List .note files under /Note/ (recursive)
for folder_path, note in api.list_notes(c):
    print(note.id, f"{folder_path}/{note.file_name}")

# Build the same markdown the CLI prints. `output` is optional:
#   None (default) → no PNG persisted; transcripts/blockquote only.
#   PathLike       → PNG(s) written; markdown includes image refs.
#                    For digests, suffix `.png` selects file mode.
md = api.render_digest_markdown(c, digest)                         # just the blockquote
md = api.render_digest_markdown(c, digest, "ann.png", ocr_engine="ollama")  # PNG + Ollama
md = api.render_note_markdown(c, file_id)                          # device transcripts only
md = api.render_note_markdown(c, file_id, "/tmp/mynote", ocr_engine="ollama")  # PNGs + Ollama

# Group digests by source document (PDF/EPUB) and get full Digest records
for src in api.list_digested_sources(c, days_ago=30):
    print(src.source_stem, len(src.digests))
    # Lower-level: render handwriting PNGs only (no OCR)
    for d in src.digests:
        paths = api.render_handwriting(c, d, "/tmp/hw")  # writes {digest_id}.png
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

- v0.3 (breaking): `digest <id>` / `note <id>` defaults are minimal — no PNGs persisted, no Ollama. Stdout is just the blockquote (digest) or the device transcript per page (note). Pass `-o PATH` to persist PNGs (digest accepts `file.png` or a dir; note accepts a dir). Pass `--ocr {supernote,ollama}` (default `supernote`) to control transcription; `--ocr ollama` runs vision OCR (replacing the old `--no-ocr` boolean). When a digest has untranscribed handwriting and no flags pull it, a one-line hint is printed to stderr. `--dir` renamed to `-o/--output`. `note <TARGET>` now accepts a basename (e.g. `20260501_073927` with or without `.note`), a full path (`Note/sub/foo.note`), or a numeric id; `note ls` shows `mtime  name`, sorted newest-last. `render_digest_markdown` and `render_note_markdown` take an optional `output` arg and `ocr_engine="supernote"|"ollama"`. `render_handwriting` writes `{digest_id}.png` / `{digest_id}_pN.png`. New API: `api.resolve_note(client, target)` returns the matching `Note` (raises `NoteNotFound` / `NoteAmbiguous`).
- v0.2 (breaking): standardized `-o/--output` across commands, path-based `download` / `delete` (with `--by-id` fallback), JSON-always output for `digest <id>` / `note <id>` using Supernote terms (`digest` / `annotation` / `handwritten_image`), new `upload` and `delete` verbs.
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
