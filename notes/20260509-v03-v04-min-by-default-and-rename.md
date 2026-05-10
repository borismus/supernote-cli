# v0.3 minimal-by-default + v0.4 rename

**Date:** 2026-05-09

Two coordinated changes that landed close together. v0.3 flipped CLI defaults to be cheap and Ollama-free; v0.4 renamed the subcommands so the surface reads correctly.

## Why

### v0.3: defaults were doing too much

The v0.2 principle was "OCR is default-on — the annotation text is the whole point." That worked when you only ran the CLI in interactive sessions to see one digest at a time. As the wrapper scripts in `note-vault-utils` started piping through it (hourly cron, batch transcription), default-on became a tax: every invocation spun Ollama whether or not the caller wanted the transcript.

Fixed by inverting the default: v0.3 makes the typical command print the blockquote / device transcript and quit. OCR is opt-in via `--ocr ollama`. Result:

- `supernote digest <id>` is now fast and side-effect-free.
- `supernote note <id>` returns the device's pre-existing transcript without ever calling Ollama.
- Persistence is opt-in via `-o PATH` instead of always-on under a default dir.

A stderr hint surfaces when handwriting exists but isn't being pulled (`note: digest X has untranscribed handwriting; pass --ocr ollama to transcribe`), so the "minimal" default doesn't silently drop information.

### v0.4: the subcommand names were misleading

`supernote note` was redundant — Supernote-the-product is itself a notebook, so "note" inside `supernote` reads like `gitlab git`. `supernote digest` was inaccurate too: the artifact a digest represents is the user's *annotation* of a passage, not a textual digest in the conventional sense.

Renamed:

- `note` → `notebook` (alias `nb`) — describes the `.note` file format honestly.
- `digest` → `annotation` (alias `an`) — matches what users mark up: a passage with handwritten ink on top.

The library/api keeps the original names (`render_digest_markdown`, `Digest` dataclass, `list_digested_sources`). That's intentional — `note-vault-utils` and any other library callers shouldn't churn for a CLI rename. The CLI translates at the dispatcher layer (`ocr_engine="ollama" if args.ocr else "none"`).

## Listing surface

`ls` was the bottleneck for ID-based addressing — you couldn't look up an ID if the listing didn't show it. Both lists now lead with the snowflake ID:

- `notebook ls`: ` {id}  {mtime}  {name}` (3 cols, 1-char left margin, no fixed-width padding).
- `annotation ls`: grouped by source. The source filename prints flush-left as a header; annotation rows under it are indented by 1 char (` {id}  {mtime}  {fragment}`). Sources are sorted oldest-first by most-recent activity, and annotations within a source are oldest-first too — so the latest entry lands right above the prompt (matches `nb ls`). Fragment text is truncated to `terminal_cols - len(prefix) - marker_w - 1` (1-char right margin). Rows whose `digest.has_annotation` is True get a trailing ` (A)` marker; the 4-char marker width is reserved on every row so when present the marker always lands at the same column. Rows without a marker have right-padding (via `ljust`) so the truncation column is consistent across rows.
- `ls` and `source ls`: 1-char left margin too.

All four list commands use a single `_ls_format_time` helper that produces macOS-`ls -l`-style timestamps: `Mon DD HH:MM` for recent (<6mo), `Mon DD  YYYY` for older (using `%e` for space-padded day so widths match). Local timezone — the api's datetime objects already come from `datetime.fromtimestamp(...)` which is local-tz by default.

The "paths, not ids" preference from v0.2 is relaxed: IDs are now the first-class addressing scheme for both, with paths/basenames kept as a fallback for `notebook` (where filenames are sometimes meaningful, e.g. `San Francisco Note, April 20`).

## `--ocr` shape asymmetry

Different defaults for different reasons:

| Subcommand | Flag shape | Default | Why |
|---|---|---|---|
| `notebook` | `--ocr {supernote,ollama}` | `supernote` | Both engines are real (device transcript vs Ollama vision). User picks. |
| `annotation` | `--ocr` (boolean) | off | Only one engine is real (the device doesn't OCR digest handwriting). Boolean is honest. |

Forcing the same shape on both would have been worse: `annotation --ocr supernote` is a nonsense incantation, and `notebook --ocr` (boolean) loses the device-transcript option.

## Cache md filename

In v0.3, `digest <id> -o DIR --ocr ollama` wrote `DIR/content.md`. v0.4 changes it to `DIR/{annotation_id}.md`. The PNG was already ID-keyed (`{annotation_id}.png`), so this just brings the cache md into the same scheme. Now separate single-id runs into the same `-o DIR` don't overwrite each other's cache md.

The notebook cache md stays `content.md` because `notebook <id>` is single-target by construction (one notebook per invocation).

## Stderr hint format

The hint that fires when handwriting exists and OCR is off is now visually separated from stdout by a leading newline, capitalized, and uses the new "annotation has untranscribed digest" wording:

```
> ...the highlighted passage...
[blank line — stderr's leading \n]
Note: annotation 832783801452068864 has untranscribed digest; pass --ocr to transcribe (or -o PATH to save the PNG)
```

The wording is asymmetric with the dataclass/api naming — there, the `Digest` dataclass IS the umbrella record and `has_annotation` is the flag for whether handwriting exists. In the CLI/UI: `annotation` is the umbrella, `digest` (within an annotation) is the textual distillation that OCR produces. The two terminology systems coexist because flipping the api would churn downstream callers.

## Tests

Smoke tests in `tests/test_smoke_live.py` were updated to match the new flag/subcommand names. The notebook tests had been stale since the v0.2 → v0.3 transition (using `--no-ocr` / `--dir` flags that no longer exist) and were fixed in the same pass. The deprecation alias `--ocr supernote` for annotations was briefly added during v0.4 then removed when the user opted for a clean break — no annotation tests cover the legacy path because there isn't one.

## Out of scope

- Multi-id `digest a,b,c -o DIR` is supported by the `{annotation_id}.md` naming change in principle, but the existing error gate is kept: multi-id is unsupported in `--ocr ollama` mode by policy.
- Multi-page annotation handling (`{id}_pN.png`) exists in code but isn't a focus.
- API rename to mirror the CLI (`render_annotation_markdown`, etc.). Deferred indefinitely; the divergence is documented but not removed.
