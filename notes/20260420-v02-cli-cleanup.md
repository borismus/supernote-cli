# v0.2 CLI cleanup + upload

**Date:** 2026-04-20

## Why

The v0.1 CLI accreted inconsistencies: three spellings of the output flag
(`--output`, `--out`, `-o/--out`), id-vs-path addressing mixed between
sibling verbs, and `digest <id>` / `note <id>` each doing three things at
once with per-flag `(ls only)` / `(render only)` tags muddling the help
text. Upload was missing entirely — no way to push a file back to the
cloud from the CLI. Pre-1.0 and not on PyPI yet, so a breaking cleanup
was cheap.

## Design principles

- **Noun groups get subcommands, file verbs stay flat.** `digest` / `note`
  / `source` use a positional `target`; top-level `ls` / `download` /
  `upload` / `sync` remain verbs.
- **Paths, not ids.** Everything user-facing addresses by path. `download`
  keeps `--by-id` as an escape hatch.
- **OCR is default-on** for `digest <id>` and `note <id>` — the
  annotation text is the whole point.
- **Content commands emit JSON.** Formatters (Readwise export, etc.) are
  someone else's job; pipe to `jq` / a script.
- **Supernote's own terminology** for fields: `digest` (the highlight),
  `annotation` (handwritten note OCR'd), `handwritten_image` (PNG path).

## Ollama behavior

- Preflight `GET {OLLAMA_HOST}/api/tags` when OCR is requested. Fail
  hard with exit 2 if unreachable — no silent data loss.
- `--no-ocr` skips preflight entirely.
- Per-image errors mid-run: capture the first error to stderr, set
  "OCR disabled for the rest of this run", emit `annotation: null` for
  remaining items. Exit 0 — partial results still print.

## Upload flow

Reverse-engineered from browser DevTools capture against
`cloud.supernote.com` (works the same on `viewer.supernote.com` which
the rest of the client uses). Three steps:

1. `POST /api/file/upload/apply` with `{size, fileName, directoryId, md5}`
   plus `nonce` and `timestamp` headers. Response fields we rely on:
   - `url` — signed S3 URL (innerName embedded in path when
     response-level `innerName` is `null`)
   - `s3Authorization` — AWS SigV4 header value (server-side signed)
   - `xamzDate` — the `x-amz-date` header value
2. `PUT` the file bytes to the signed URL with
   `Authorization` / `x-amz-date` / `x-amz-content-sha256: UNSIGNED-PAYLOAD` /
   `Content-Type: application/x-www-form-urlencoded` (yes, that's what
   the server signs for; deviating from it breaks the signature).
3. `POST /api/file/upload/finish` with
   `{directoryId, fileName, fileSize, innerName, md5}`.

Overwrite semantics: we pre-list the destination, fail if the filename
exists unless `--overwrite`, and when `--overwrite` we call `delete_file`
then **poll the listing** until the filename is gone before starting the
new apply/PUT/finish cycle. Without the poll, `finish` races the
server-side deletion and returns a generic "Server Error".

## Delete endpoint

`POST /api/file/delete` with payload:

```json
{"idList": ["<file_id>"], "directoryId": "<parent_dir_id>"}
```

Both fields are required — the server validates them with two separate
messages ("File ID list cannot be empty" + "The ID of the file root
directory cannot be empty"). Field name is `idList` (not `fileIds`,
`fileIdList`, or `ids` — all of which the server silently treats as the
list being empty). This took a few guesses to land on.

## Out of scope

- `mkdir` verb (deferred; use the Supernote web UI).
- Glob / multi-file / recursive upload.
- Back-compat aliases for v0.1 verbs.
- Progress bars for large transfers.
- Readwise / markdown formatters.
