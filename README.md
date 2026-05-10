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

supernote annotation ls [--limit N] [--days-ago N] [--json]   # alias: an
supernote annotation <id> \                                   # blockquote to stdout; nothing written
         [-o PATH] [--ocr {none,ollama}] [--model M] [--force] [--json] [--prompt TEXT]

supernote notebook ls [--days-ago N] [--limit N] [--json]     # alias: nb
supernote notebook <id|name|path> \                           # device transcript to stdout; nothing written
         [-o DIR] [--ocr {supernote,ollama}] [--model M] [--force] [--json] [--prompt TEXT]
```

Global flags: `--no-cache`, `--verbose`, `--equipment-no`.

### Addressing

Most commands take a remote path (`Note/Inbox/foo.note`). `download` and `delete` also accept `--by-id <ID>` as an escape hatch. `upload` expects the destination folder to already exist — it won't create missing folders. `delete` removes the remote file immediately with no confirmation prompt; `upload --overwrite` uses it internally and waits for the deletion to propagate server-side before re-uploading.

### `annotation <id>` (alias `an`) — blockquote to stdout; `-o` for the PNG, `--ocr` for LLM transcription

By default, `annotation <id>` prints just the highlighted passage. Nothing is written to disk and no Ollama call is made:

```
$ supernote annotation 832783777540341760
> I've decided to throw my chemoreceptors into the ring...
```

- The `>` block is the highlighted passage Supernote already transcribed (`digest.content`).
- If the annotation also has handwriting, a hint is printed to **stderr**: `Note: annotation <id> has untranscribed digest; pass --ocr to transcribe (or -o PATH to save the PNG)`. Stdout stays clean for piping.

`annotation ls` groups annotations by their source document. Each source's filename prints once as a header; annotation rows under it are indented with `{id}  {mtime}  {fragment}`. Sources are sorted by most-recent activity oldest-first, annotations within a source by mtime ascending — so the latest entry lands right above your prompt (same convention as `nb ls`). Timestamps follow macOS `ls -l` style: `Mon DD HH:MM` for recent (<6mo), `Mon DD  YYYY` for older. Fragment text is truncated to fit the terminal width minus a 1-char right margin. Rows whose underlying digest has a handwritten annotation on top get a trailing `(A)` marker (always at the same column, so it's easy to scan).

```
$ supernote an ls --limit 6
Breath_The_New_Science_of_a_Lost_Art_James_Nestor.pdf
 833859949221117952  Apr 18 17:29  BREATHING COORDINATION This technique he
 833859951360212992  Apr 18 17:30  RESONANT (COHERENT) BREATHING A calming  (A)
 833859952597532672  Apr 18 17:34  It's important that the first breath in  (A)
 833859955948781568  Apr 18 17:45  TUMMO There are two forms of Tummo—one t (A)
 833859956464680960  Apr 18 17:49  Breathhold Walking Anders Olsson uses th
 833859957852995584  Apr 18 18:00  Close the mouth and inhale quietly throu (A)
```

The lines without `(A)` are highlight-only — the user marked the passage but didn't scrawl a note on top. Pass `annotation <id> --ocr` on the `(A)` ones to get the handwriting transcribed.

Pass `-o PATH` to persist the rendered handwriting PNG. `PATH` ending in `.png` is file mode; otherwise it's a directory:

```
$ supernote annotation 832783777540341760 -o ann.png
> I've decided to throw my chemoreceptors into the ring...

_(no transcript)_

![](ann.png)
```

```
$ supernote annotation 832783777540341760 -o annotations/
> I've decided to throw my chemoreceptors into the ring...

_(no transcript)_

![](annotations/832783777540341760.png)
```

In dir mode the markdown cache is keyed by id too — `{output}/{annotation_id}.md` — so separate single-id runs into the same dir don't overwrite each other.

Pass `--ocr` to transcribe the handwriting via local Ollama vision OCR. With `-o`, the OCR text replaces the placeholder and a sibling `{annotation_id}.md` (file mode: `{stem}.md`) is written as a cache marker:

```
$ supernote annotation 832783777540341760 --ocr -o ann.png
> I've decided to throw my chemoreceptors into the ring...

could this be something for dad to look into?

![](ann.png)
```

Without `-o`, `--ocr` still works — the PNG renders to a tempdir, gets OCR'd, and is discarded. Stdout is just `blockquote + ocr text`.

Pass `--json` for the structured shape:

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

`handwritten_image` is `null` unless `-o` was passed. `annotation` (the OCR'd handwriting) is `null` unless `--ocr` was passed.

Multiple comma-separated IDs print one block per annotation (or a JSON array). `-o file.png` requires a single id; `-o dir/ --ocr` is also single-id today.

### `notebook <id|name|path>` (alias `nb`) — device transcript to stdout; `-o` for page PNGs, `--ocr ollama` for LLM transcription

The target is resolved in this order:

- **All-digits** → numeric file id (recommended, since `notebook ls` surfaces them).
- **Contains `/`** → full path under `Note/` (or absolute `Note/sub/foo.note`).
- **Otherwise** → basename (with or without `.note` suffix). If multiple notes share that basename across folders, the resolver errors with the matching paths so you can disambiguate.

`notebook ls` prints 3 columns — id, last-modified date, name — sorted oldest-first / newest-last:

```
$ supernote nb ls --limit 5
1254057731111780353  Apr 24 08:11  San Francisco Note, April 20
1254579462477971456  Apr 27 07:36  20260424_081053
1256465358412316672  May  1 07:39  20260429_132435
1258756940570296320  May  5 21:10  Eliana 2
1257109318499565568  May  5 21:21  20260501_073927
$ supernote notebook 1257109318499565568
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

### `--ocr` flags

The two subcommands have different `--ocr` shapes — what makes sense for each artifact:

| Subcommand | Flag shape | Default | Notes |
|---|---|---|---|
| `notebook` | `--ocr {supernote,ollama}` | `supernote` | `supernote` = per-page device transcript via `extract_note_text`; `ollama` = per-page Ollama vision OCR (replaces device transcript) |
| `annotation` | `--ocr` (boolean) | off | Off: no transcription, body has only the blockquote (with `_(no transcript)_` next to the image when `-o` is set and handwriting exists). On: Ollama vision OCR of the rendered handwriting PNG. |

The shapes differ because notebooks have two meaningful engines (device vs Ollama), while annotations only have one (the device doesn't OCR digest handwriting).

### Custom OCR prompt

`--prompt TEXT` (only meaningful when Ollama OCR is engaged: `--ocr ollama` for `notebook`, `--ocr` for `annotation`) layers project-specific transcription rules on top of the default OCR prompt. Useful for preserving inline markers verbatim:

```
$ supernote notebook <id> --ocr ollama --prompt "When a line begins with → or ☐, transcribe it verbatim including the leading symbol; preserve multi-line continuation."
```

The text is appended under an `Additional instructions:` section after the default OCR prompt. **The cache markdown does not track the prompt** (notebook: `content.md`; annotation: `{annotation_id}.md`). If you change the prompt and want fresh output, pass `--force` to invalidate.

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

- v0.4 (breaking): subcommands renamed for clarity. `note <id>` → `notebook <id>` (alias `nb`); `digest <id>` → `annotation <id>` (alias `an`). `annotation <id>` is OCR-off by default — pass bare `--ocr` (boolean) to transcribe via Ollama. `notebook ls` adds a leading id column (`{id}  {date}  {name}`). `annotation ls` becomes 4 columns (`{id}  {date}  {doc-name}  {fragment}`). `annotation`'s dir-mode cache markdown is now keyed by id (`{annotation_id}.md` instead of `content.md`) so separate single-id runs into the same dir don't collide. The untranscribed-handwriting stderr hint is now `Note: annotation <id> has untranscribed digest; pass --ocr to transcribe (or -o PATH to save the PNG)`.
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
