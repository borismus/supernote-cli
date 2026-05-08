"""High-level API: endpoint wrappers + workflows over a Client."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import random
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ocr as _ocr
from .client import ApiError, Client
from .models import Digest, DigestHash, Note


def list_files(client: Client, directory_id: str | int = 0, page_size: int = 500) -> list[Note]:
  data = client._post(
    "file/list/query",
    {
      "directoryId": directory_id,
      "pageNo": 1,
      "pageSize": page_size,
      "order": "time",
      "sequence": "desc",
    },
  )
  return [Note.from_api(it) for it in data.get("userFileVOList") or []]


def resolve_path(client: Client, path: str) -> tuple[str | int, list[Note]]:
  """Walk a slash-separated path from root, returning (directoryId, contents).

  Empty string or "/" is the root.
  """
  parts = [p for p in (path or "").split("/") if p]
  directory_id: str | int = 0
  contents = list_files(client, directory_id)
  for i, part in enumerate(parts):
    match = next((n for n in contents if n.is_folder and n.file_name == part), None)
    if match is None:
      walked = "/".join(parts[:i]) or "(root)"
      raise ApiError(f"path component '{part}' not found under {walked}")
    directory_id = match.id
    contents = list_files(client, directory_id)
  return directory_id, contents


def download_url(client: Client, file_id: str | int) -> str:
  data = client._post("file/download/url", {"id": file_id, "type": 0})
  url = data.get("url")
  if not url:
    raise ApiError(f"no download url returned for id={file_id}")
  return url


def download_file(client: Client, file_id: str | int, dest: Path) -> int:
  url = download_url(client, file_id)
  return client.download_to(url, dest)


def delete_file(client: Client, note: Note) -> None:
  """Delete a remote file. Requires a `Note` (not just an id) because the
  endpoint wants `directoryId` alongside the file id list."""
  client._post(
    "file/delete",
    {
      "idList": [str(note.id)],
      "directoryId": str(note.directory_id) if note.directory_id else "0",
    },
  )


def resolve_file(client: Client, path: str) -> Note:
  """Resolve a slash-separated remote path to the `Note` for its leaf file.

  The last path component must be a file (not a folder); raises `ApiError`
  otherwise.
  """
  parts = [p for p in (path or "").split("/") if p]
  if not parts:
    raise ApiError("empty path; expected a file path like 'Note/Inbox/foo.note'")
  parent_path = "/".join(parts[:-1])
  leaf = parts[-1]
  _, contents = resolve_path(client, parent_path)
  match = next((n for n in contents if not n.is_folder and n.file_name == leaf), None)
  if match is None:
    where = parent_path or "(root)"
    raise ApiError(f"file '{leaf}' not found in {where}")
  return match


def _md5_file(path: Path) -> str:
  h = hashlib.md5()
  with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
      h.update(chunk)
  return h.hexdigest()


def _upload_apply_headers() -> dict:
  ts = int(time.time() * 1000)
  nonce = f"{random.randint(10**9, 10**10 - 1)}{ts}"
  return {"nonce": nonce, "timestamp": str(ts)}


def upload_file(
  client: Client,
  local_path: str | os.PathLike,
  remote_dir: str,
  *,
  overwrite: bool = False,
) -> Note:
  """Upload a local file to a remote directory.

  Flow: `file/upload/apply` -> signed PUT to S3 -> `file/upload/finish`.
  `remote_dir` must resolve to an existing folder; no auto-create.

  If a file with the same name already exists in `remote_dir`:
    - `overwrite=False` raises `ApiError("already exists: ...")`
    - `overwrite=True` deletes the existing file first via `file/delete`.

  Returns the `Note` for the newly uploaded file.
  """
  src = Path(local_path)
  if not src.is_file():
    raise ApiError(f"local file not found: {src}")

  directory_id, contents = resolve_path(client, remote_dir)
  existing = next(
    (n for n in contents if not n.is_folder and n.file_name == src.name), None
  )
  if existing is not None:
    if not overwrite:
      raise ApiError(f"already exists: {remote_dir.rstrip('/')}/{src.name}")
    delete_file(client, existing)
    # Delete is async on the server side; wait for the listing to reflect
    # the absence before we start a new apply/PUT/finish cycle, otherwise
    # finish fails with "Server Error" on the not-yet-propagated filename.
    for _ in range(20):
      _, check = resolve_path(client, remote_dir)
      if not any(n.file_name == src.name and not n.is_folder for n in check):
        break
      time.sleep(0.25)
    else:
      raise ApiError(
        f"timed out waiting for deletion of {remote_dir.rstrip('/')}/{src.name} to propagate"
      )

  size = src.stat().st_size
  md5 = _md5_file(src)
  apply_payload = {
    "size": size,
    "fileName": src.name,
    "directoryId": str(directory_id) if directory_id else "0",
    "md5": md5,
  }
  apply_resp = client._post(
    "file/upload/apply", apply_payload, extra_headers=_upload_apply_headers()
  )

  signed_url = apply_resp.get("url") or apply_resp.get("signedUrl")
  if not signed_url:
    raise ApiError(f"upload/apply: no signed url in response: {apply_resp}")

  inner_name = apply_resp.get("innerName") or apply_resp.get("fileKey")
  if not inner_name:
    # Server often returns innerName as null and embeds it in the URL path.
    inner_name = signed_url.rsplit("/", 1)[-1].split("?", 1)[0]

  put_headers = _extract_s3_headers(apply_resp)
  client.put_binary(signed_url, src, put_headers)

  finish_payload = {
    "directoryId": str(directory_id) if directory_id else "0",
    "fileName": src.name,
    "fileSize": size,
    "innerName": inner_name,
    "md5": md5,
  }
  finish_resp = client._post("file/upload/finish", finish_payload)
  note_data = (
    finish_resp.get("userFileVO")
    or finish_resp.get("data")
    or finish_resp.get("file")
  )
  if note_data:
    return Note.from_api(note_data)

  # Fallback: re-list the directory and find our file.
  _, contents = resolve_path(client, remote_dir)
  match = next(
    (n for n in contents if not n.is_folder and n.file_name == src.name), None
  )
  if match is None:
    raise ApiError(
      f"upload finished but '{src.name}' not found in {remote_dir}; "
      f"response: {finish_resp}"
    )
  return match


def _extract_s3_headers(apply_resp: dict) -> dict:
  """Pull the S3 PUT headers out of an upload/apply response."""
  auth = (
    apply_resp.get("s3Authorization")
    or apply_resp.get("authorization")
    or apply_resp.get("Authorization")
  )
  amz_date = (
    apply_resp.get("xamzDate")
    or apply_resp.get("xAmzDate")
    or apply_resp.get("amzDate")
    or apply_resp.get("x-amz-date")
  )
  if not auth or not amz_date:
    raise ApiError(f"upload/apply: missing signing headers in response: {apply_resp}")
  return {
    "Authorization": auth,
    "x-amz-date": amz_date,
    "x-amz-content-sha256": "UNSIGNED-PAYLOAD",
    "Content-Type": "application/x-www-form-urlencoded",
  }


@dataclass
class SyncEntry:
  note: Note
  local_path: Path
  action: str  # "download" | "skip:uptodate" | "skip:filter"


def sync_folder(
  client: Client,
  folder_path: str,
  out_dir: str | os.PathLike,
  *,
  days_ago: int | None = None,
  dry_run: bool = False,
  recursive: bool = False,
) -> list[SyncEntry]:
  """Mirror a folder's .note files into out_dir.

  Preserves the API's updateTime as local mtime. Skips files whose local mtime
  already matches the remote updateTime within 1 second. Optionally filters by
  how recently the note was updated.
  """
  out = Path(out_dir)
  out.mkdir(parents=True, exist_ok=True)

  directory_id, contents = resolve_path(client, folder_path)

  cutoff: dt.datetime | None = None
  if days_ago is not None:
    cutoff = dt.datetime.now() - dt.timedelta(days=days_ago)

  results: list[SyncEntry] = []
  for note in contents:
    if note.is_folder:
      if recursive:
        nested = sync_folder(
          client,
          f"{folder_path.rstrip('/')}/{note.file_name}",
          out / note.file_name,
          days_ago=days_ago,
          dry_run=dry_run,
          recursive=True,
        )
        results.extend(nested)
      continue

    if cutoff is not None and note.update_time < cutoff:
      results.append(SyncEntry(note, out / note.file_name, "skip:filter"))
      continue

    local = out / note.file_name
    remote_ts = note.update_time.timestamp()
    if local.exists() and abs(local.stat().st_mtime - remote_ts) < 1:
      results.append(SyncEntry(note, local, "skip:uptodate"))
      continue

    if not dry_run:
      download_file(client, note.id, local)
      os.utime(local, (remote_ts, remote_ts))
    results.append(SyncEntry(note, local, "download"))

  return results


def fetch_digest_hashes(
  client: Client,
  *,
  page: int = 1,
  size: int = 500,
  parent_unique_identifier: str | None = None,
) -> list[DigestHash]:
  data = client._post(
    "file/query/summary/hash",
    {
      "ids": None,
      "page": page,
      "parentUniqueIdentifier": parent_unique_identifier,
      "size": size,
    },
    include_channel=True,
  )
  return [DigestHash.from_api(r) for r in data.get("summaryInfoVOList") or []]


def fetch_digests_by_ids(client: Client, ids: list[str | int]) -> list[Digest]:
  if not ids:
    return []
  data = client._post(
    "file/query/summary/id",
    {"ids": ids},
    include_channel=True,
  )
  return [Digest.from_api(r) for r in data.get("summaryDOList") or []]


@dataclass
class SourceDigests:
  source_path: str
  source_stem: str
  digests: list[Digest]
  latest_modified: dt.datetime


def list_digested_sources(
  client: Client,
  *,
  days_ago: int | None = None,
  source_path: str | None = None,
  batch_size: int = 100,
) -> list[SourceDigests]:
  """List source documents that have digests, most-recent first.

  Fetches digest hashes, optionally filters by `last_modified`, chunk-fetches
  the full digests, groups by `source_path`, and returns a list of
  `SourceDigests` sorted by `latest_modified` descending.

  Digests with empty `source_path` are dropped. Passing `source_path` returns
  a 1- or 0-element list (filtered in-memory).
  """
  hashes = fetch_digest_hashes(client, size=500)

  if days_ago is not None:
    cutoff = dt.datetime.now() - dt.timedelta(days=days_ago)
    hashes = [h for h in hashes if h.last_modified >= cutoff]

  hash_by_id = {h.id: h for h in hashes}

  digests: list[Digest] = []
  ids = list(hash_by_id.keys())
  for i in range(0, len(ids), batch_size):
    chunk = fetch_digests_by_ids(client, ids[i : i + batch_size])
    for d in chunk:
      if d.last_modified_time is None:
        h = hash_by_id.get(d.id)
        if h is not None:
          d.last_modified_time = h.last_modified
      digests.append(d)

  grouped: dict[str, list[Digest]] = {}
  for d in digests:
    if not d.source_path:
      continue
    if source_path is not None and d.source_path != source_path:
      continue
    grouped.setdefault(d.source_path, []).append(d)

  out: list[SourceDigests] = []
  for path, group in grouped.items():
    group.sort(key=lambda d: d.last_modified_time or dt.datetime.min, reverse=True)
    latest = max(
      (d.last_modified_time for d in group if d.last_modified_time),
      default=dt.datetime.min,
    )
    out.append(
      SourceDigests(
        source_path=path,
        source_stem=PurePosixPath(path).stem,
        digests=group,
        latest_modified=latest,
      )
    )
  out.sort(key=lambda s: s.latest_modified, reverse=True)
  return out


def fetch_handwriting_url(client: Client, digest_id: str | int) -> str | None:
  """Ask the API for a signed URL to the per-highlight handwriting .mark file.

  Returns None if the digest has no associated handwriting (the highlight
  was never annotated). Other errors raise.
  """
  data = client._post("file/download/summary", {"id": digest_id}, include_channel=True)
  return data.get("url")


def render_handwriting(
  client: Client,
  digest: Digest,
  dir: str | os.PathLike,
  *,
  force: bool = False,
) -> list[Path]:
  """Render a digest's handwriting to `{digest_id}.png` inside `dir`.

  Single-page handwriting is written as `{digest_id}.png`; multi-page is
  written as `{digest_id}_p{N}.png` (1-indexed).

  Returns the full list of page PNG paths (one per rendered page), whether
  newly written or already on disk. Empty list if the digest has no
  handwriting (`has_annotation` is False).

  Skips pages whose PNG already exists unless `force=True`.
  """
  from supernotelib import load_notebook
  from supernotelib.converter import ImageConverter

  if not digest.has_annotation:
    return []
  url = fetch_handwriting_url(client, digest.id)
  if not url:
    return []

  out = Path(dir)
  out.mkdir(parents=True, exist_ok=True)
  mark_bytes = client.get_binary(url)

  with tempfile.NamedTemporaryFile(suffix=".mark") as tmp:
    tmp.write(mark_bytes)
    tmp.flush()
    notebook = load_notebook(tmp.name)
    total = notebook.get_total_pages()
    converter = ImageConverter(notebook, palette=None)

    paths: list[Path] = []
    for i in range(total):
      suffix = "" if total == 1 else f"_p{i + 1}"
      dest = out / f"{digest.id}{suffix}.png"
      paths.append(dest)
      if dest.exists() and not force:
        continue
      img = converter.convert(i)
      if img is None:
        continue
      img.save(dest)
    return paths


@dataclass
class NotePage:
  index: int  # 1-based
  png_path: Path
  transcript: str | None  # supernotelib device-OCR (may be None)
  ocr_text: str | None  # local Ollama OCR (None if disabled/failed)


def render_note(
  note_path: str | os.PathLike,
  out_dir: str | os.PathLike,
  *,
  force: bool = False,
) -> list[Path]:
  """Render each page of a `.note` file to `page_{N}.png` (1-indexed).

  Creates `out_dir` if needed. Skips existing PNGs unless `force=True`.
  Returns the full list of page PNG paths (one per page, indexed by
  page number), whether newly written or already on disk.
  """
  from supernotelib import load_notebook
  from supernotelib.converter import ImageConverter

  out_path = Path(out_dir)
  out_path.mkdir(parents=True, exist_ok=True)

  notebook = load_notebook(str(note_path))
  total = notebook.get_total_pages()
  converter = ImageConverter(notebook, palette=None)

  paths: list[Path] = []
  for i in range(total):
    dest = out_path / f"page_{i + 1}.png"
    paths.append(dest)
    if dest.exists() and not force:
      continue
    img = converter.convert(i)
    if img is None:
      continue
    img.save(dest)
  return paths


def extract_note_text(note_path: str | os.PathLike) -> list[str]:
  """Return per-page device-OCR transcripts via supernotelib.

  List length matches `notebook.get_total_pages()`. A page's entry is an
  empty string if the device produced no transcript (or the converter
  returned None).
  """
  from supernotelib import load_notebook
  from supernotelib.converter import TextConverter

  notebook = load_notebook(str(note_path))
  total = notebook.get_total_pages()
  converter = TextConverter(notebook, palette=None)
  return [converter.convert(i) or "" for i in range(total)]


def ocr_note(
  note_path: str | os.PathLike,
  out_dir: str | os.PathLike,
  *,
  model: str = _ocr.DEFAULT_MODEL,
  force: bool = False,
  extra_prompt: str | None = None,
  on_page_start: Callable[[int, int], None] | None = None,
  on_token: Callable[[str], None] | None = None,
) -> list[NotePage]:
  """Render a `.note` file's pages, pull device transcripts, OCR each page.

  Bundles `render_note` + `extract_note_text` + `ocr_image` into one call.
  If Ollama is unreachable, `ocr_text` is None on every page but the rest
  of the record is still populated. `extra_prompt` is forwarded to
  `ocr_image` for project-specific transcription rules.

  Two structured progress callbacks (both optional):
    on_page_start(page_index, total_pages): once per page before OCR.
    on_token(delta): per Ollama streaming chunk (also includes any
      thinking text the model emits). Returned string is unaffected.
  """
  png_paths = render_note(note_path, out_dir, force=force)
  transcripts = extract_note_text(note_path)
  total = len(png_paths)

  pages: list[NotePage] = []
  for i, png in enumerate(png_paths):
    if on_page_start:
      on_page_start(i + 1, total)
    transcript = transcripts[i] if i < len(transcripts) else ""
    ocr_text: str | None = None
    if png.exists():
      try:
        ocr_text = _ocr.ocr_image(
          png, model=model, extra_prompt=extra_prompt, on_token=on_token
        )
      except _ocr.OcrError:
        ocr_text = None
    pages.append(
      NotePage(
        index=i + 1,
        png_path=png,
        transcript=transcript or None,
        ocr_text=ocr_text,
      )
    )
  return pages


def ocr_note_from_cloud(
  client: Client,
  file_id: str | int,
  out_dir: str | os.PathLike,
  *,
  model: str = _ocr.DEFAULT_MODEL,
  force: bool = False,
  extra_prompt: str | None = None,
  on_page_start: Callable[[int, int], None] | None = None,
  on_token: Callable[[str], None] | None = None,
) -> list[NotePage]:
  """Download a cloud `.note` by id to a temp file, then run `ocr_note`.

  PNG outputs persist under `out_dir`; the downloaded `.note` is discarded
  after rendering. `extra_prompt`, `on_page_start`, and `on_token` are
  forwarded to `ocr_note`.
  """
  with tempfile.NamedTemporaryFile(suffix=".note", delete=True) as tmp:
    download_file(client, file_id, Path(tmp.name))
    return ocr_note(
      tmp.name,
      out_dir,
      model=model,
      force=force,
      extra_prompt=extra_prompt,
      on_page_start=on_page_start,
      on_token=on_token,
    )


class NoteNotFound(Exception):
  pass


class NoteAmbiguous(Exception):
  def __init__(self, target: str, matches: list[str]):
    self.target = target
    self.matches = matches
    super().__init__(f"target '{target}' matches multiple notes: {', '.join(matches)}")


def resolve_note(client: Client, target: str) -> Note:
  """Resolve a `note` CLI target to a `Note` record.

  Accepts:
    - a numeric id (all digits)
    - a full path: `Note/.../foo.note`
    - a basename: `foo.note` or `foo` (`.note` suffix optional)

  Raises NoteNotFound or NoteAmbiguous on lookup failure.
  """
  pairs = list_notes(client, folder_path="Note", recursive=True)

  if target.isdigit():
    for _, n in pairs:
      if n.id == target:
        return n
    raise NoteNotFound(f"no note with id {target}")

  if "/" in target:
    needle = target.lstrip("/")
    if needle.startswith("Note/"):
      needle = needle[len("Note/"):]
    for fp, n in pairs:
      rel = fp[len("Note"):].lstrip("/")
      full_rel = f"{rel}/{n.file_name}" if rel else n.file_name
      if full_rel == needle:
        return n
    raise NoteNotFound(f"no note at path {target}")

  basename = target if target.endswith(".note") else f"{target}.note"
  matches = [(fp, n) for fp, n in pairs if n.file_name == basename]
  if not matches:
    raise NoteNotFound(f"no note named {basename}")
  if len(matches) > 1:
    paths = [f"{fp[len('Note'):].lstrip('/')}/{n.file_name}".lstrip("/") for fp, n in matches]
    raise NoteAmbiguous(target, paths)
  return matches[0][1]


def list_notes(
  client: Client,
  folder_path: str = "Note",
  *,
  recursive: bool = True,
) -> list[tuple[str, Note]]:
  """Return `(folder_path, Note)` pairs for every `.note` file under a folder.

  Recursive by default. The accompanying `folder_path` is the slash-joined
  breadcrumb from the root to the note's containing folder (so callers can
  reconstruct a display name).
  """
  _, contents = resolve_path(client, folder_path)
  out: list[tuple[str, Note]] = []
  for n in contents:
    if n.is_folder:
      if recursive:
        child_path = f"{folder_path.rstrip('/')}/{n.file_name}"
        out.extend(list_notes(client, child_path, recursive=True))
      continue
    if n.file_name.endswith(".note"):
      out.append((folder_path, n))
  return out


# ---- Markdown helpers (digest + note) ----


_IMAGE_REF_RE = re.compile(r"^!\[\]\([^)]+\)\s*$")
NO_TRANSCRIPT_PLACEHOLDER = "_(no transcript)_"


def _compose_digest_markdown(
  highlight: str,
  ocr_body: str,
  image_refs: list[str] | None = None,
) -> str:
  """Build digest markdown: blockquoted highlight + optional OCR + image refs.

  `image_refs` are markdown-relative paths to handwriting PNGs; each is
  appended as `![](ref)` after the body (or after the blockquote when
  there is no body), separated by a blank line.
  """
  if highlight:
    quoted = "\n".join(f"> {line}" for line in highlight.splitlines())
  else:
    quoted = "> "
  parts = [quoted]
  if ocr_body:
    parts.append(ocr_body.rstrip())
  if image_refs:
    parts.append("\n".join(f"![]({ref})" for ref in image_refs))
  return "\n\n".join(parts) + "\n"


def _compose_note_markdown(
  pages: list[NotePage],
  image_refs: list[str] | None = None,
  *,
  empty_placeholder: str = "",
) -> str:
  """Build note markdown: one ## Page N section per page.

  Prefers Ollama OCR text when present, otherwise falls back to the
  device transcript. If both are empty and `empty_placeholder` is
  given, the placeholder is shown in the page body. When `image_refs`
  is given (parallel to `pages`), each section ends with an
  `![](ref)` line so the printed markdown points at the rendered PNG.
  """
  sections = []
  for i, p in enumerate(pages):
    text = ((p.ocr_text or p.transcript) or "").rstrip()
    if not text and empty_placeholder:
      text = empty_placeholder
    body = f"## Page {p.index}\n\n{text}\n"
    if image_refs and i < len(image_refs) and image_refs[i]:
      body += f"\n![]({image_refs[i]})\n"
    sections.append(body)
  return "\n".join(sections) if sections else ""


_NOTE_PAGE_HEADER_RE = re.compile(r"^## Page (\d+)\s*$", re.MULTILINE)


def _strip_image_refs(text: str) -> str:
  """Remove trailing markdown image-only lines (and the blank lines around them)."""
  lines = text.splitlines()
  while lines and (not lines[-1].strip() or _IMAGE_REF_RE.match(lines[-1])):
    lines.pop()
  return "\n".join(lines)


def _parse_digest_markdown(md: str) -> tuple[str, str | None]:
  """Inverse of _compose_digest_markdown.

  Returns (highlight, ocr_text_or_None). The highlight is the joined
  content of leading `> `-prefixed lines (newlines preserved); the OCR
  body is everything after the first blank line that follows the
  blockquote, with trailing `![](...)` image lines stripped. Returns
  ocr=None when no body is present, or when the body is just the
  no-transcript placeholder.
  """
  lines = md.splitlines()
  quote_lines: list[str] = []
  i = 0
  while i < len(lines) and lines[i].startswith(">"):
    quote_lines.append(lines[i][1:].lstrip(" "))
    i += 1
  highlight = "\n".join(quote_lines)
  while i < len(lines) and lines[i].strip() == "":
    i += 1
  body = _strip_image_refs("\n".join(lines[i:])).strip()
  if body == NO_TRANSCRIPT_PLACEHOLDER:
    body = ""
  return highlight, (body or None)


def _parse_note_markdown(md: str) -> list[tuple[int, str]]:
  """Inverse of _compose_note_markdown.

  Returns [(page_number, ocr_text), ...] in order of appearance. Trailing
  `![](...)` image refs in each section are stripped from the OCR text;
  a body that is exactly the no-transcript placeholder is normalized to "".
  """
  matches = list(_NOTE_PAGE_HEADER_RE.finditer(md))
  out: list[tuple[int, str]] = []
  for idx, m in enumerate(matches):
    page = int(m.group(1))
    body_start = m.end()
    body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
    body = md[body_start:body_end].strip("\n")
    if body.startswith("\n"):
      body = body[1:]
    body = _strip_image_refs(body).rstrip()
    if body == NO_TRANSCRIPT_PLACEHOLDER:
      body = ""
    out.append((page, body))
  return out


def _digest_output_mode(output: str | os.PathLike) -> tuple[bool, Path]:
  """Return (is_file_mode, path). File mode if output ends in `.png`."""
  p = Path(output)
  return (p.suffix.lower() == ".png", p)


def _digest_cache_md_path(output: str | os.PathLike) -> Path:
  is_file, p = _digest_output_mode(output)
  return p.with_suffix(".md") if is_file else p / "content.md"


def _digest_target_for_render(
  output: str | os.PathLike, rendered: list[Path], digest_id: str
) -> tuple[list[Path], list[str]]:
  """Given render_handwriting's output (`{id}.png`/`{id}_pN.png` in a dir),
  rename to the user's `output` spec and return (final_paths, image_refs).

  File mode: `output` becomes the single file (or fans to `{stem}_pN.png`).
  Dir mode: keep `{id}.png` / `{id}_pN.png` naming inside `output`.
  """
  is_file, p = _digest_output_mode(output)
  output_str = str(output)
  n = len(rendered)
  finals: list[Path] = []
  refs: list[str] = []
  if is_file:
    parent = p.parent
    stem = p.stem
    for i, src in enumerate(rendered):
      target = p if (n == 1 and i == 0) else parent / f"{stem}_p{i + 1}.png"
      finals.append(target)
      ref_parent = output_str[: -len(p.name)]
      refs.append(output_str if (n == 1 and i == 0) else f"{ref_parent}{stem}_p{i + 1}.png")
  else:
    prefix = output_str.rstrip("/") + "/"
    for src in rendered:
      finals.append(p / src.name)
      refs.append(f"{prefix}{src.name}")
  return finals, refs


def render_digest_markdown(
  client: Client,
  digest: Digest,
  output: str | os.PathLike | None = None,
  *,
  ocr_model: str = _ocr.DEFAULT_MODEL,
  ocr_engine: str = "supernote",
  force: bool = False,
  extra_prompt: str | None = None,
  on_page_start: Callable[[int, int], None] | None = None,
  on_token: Callable[[str], None] | None = None,
) -> str:
  """Build the stdout-equivalent markdown for a digest.

  When `output` is None, no PNG persists — the blockquote is the only
  text output, and Ollama (if engaged via `ocr_engine="ollama"`) runs
  against a tempdir-rendered PNG that is then discarded.

  When `output` is given:
    - ending in `.png`: single PNG written to that path; multi-page
      fan-out is `{stem}_p{N}.png` next to it.
    - otherwise: directory; PNGs written as `{digest_id}.png` /
      `{digest_id}_p{N}.png` inside.
    The returned markdown includes `![](...)` image refs pointing at
    the persisted file(s). With `ocr_engine="ollama"`, `content.md`
    is also written next to the PNG(s) (or as `{stem}.md` in file
    mode) as a cache marker.

  `ocr_engine`:
    - "supernote" (default): no annotation transcription (Supernote's
      device OCR doesn't cover digest handwriting). When `output` is
      set and handwriting exists, the body shows `_(no transcript)_`
      next to the image; without `output`, no placeholder is shown.
    - "ollama": runs Ollama vision OCR on the PNG(s).

  Progress callbacks (both optional) — same shape as ocr_note.
  """
  use_ollama = ocr_engine == "ollama"
  has_hw = digest.has_annotation
  needs_pngs = has_hw and (output is not None or use_ollama)

  if output is not None and use_ollama and not force:
    cache_md = _digest_cache_md_path(output)
    if cache_md.exists():
      return cache_md.read_text()

  ocr_body = ""
  image_refs: list[str] = []

  if needs_pngs:
    if output is None:
      with tempfile.TemporaryDirectory() as td:
        rendered = render_handwriting(client, digest, td, force=force)
        if use_ollama:
          ocr_body = _run_ollama_on_pages(
            rendered, ocr_model, extra_prompt, on_page_start, on_token
          )
    else:
      is_file, p = _digest_output_mode(output)
      work_dir = p.parent if is_file else p
      work_dir.mkdir(parents=True, exist_ok=True)
      rendered = render_handwriting(client, digest, work_dir, force=force)
      finals, image_refs = _digest_target_for_render(output, rendered, digest.id)
      _rename_to_targets(rendered, finals, force=force)
      if use_ollama:
        ocr_body = _run_ollama_on_pages(
          finals, ocr_model, extra_prompt, on_page_start, on_token
        )

  if image_refs and not ocr_body and has_hw:
    ocr_body = NO_TRANSCRIPT_PLACEHOLDER

  md = _compose_digest_markdown(digest.content or "", ocr_body, image_refs)

  if output is not None and use_ollama:
    cache_md = _digest_cache_md_path(output)
    cache_md.parent.mkdir(parents=True, exist_ok=True)
    cache_md.write_text(md)

  return md


def _run_ollama_on_pages(
  paths: list[Path],
  model: str,
  extra_prompt: str | None,
  on_page_start: Callable[[int, int], None] | None,
  on_token: Callable[[str], None] | None,
) -> str:
  """Run Ollama OCR on each PNG; return joined non-empty results, or the
  no-transcript placeholder if every page came back empty."""
  total = len(paths)
  parts: list[str] = []
  for i, p in enumerate(paths):
    if on_page_start:
      on_page_start(i + 1, total)
    parts.append(
      _ocr.ocr_image(
        p, model=model, extra_prompt=extra_prompt, on_token=on_token
      )
      or ""
    )
  body = "\n\n".join(part for part in parts if part)
  return body or NO_TRANSCRIPT_PLACEHOLDER


def _rename_to_targets(rendered: list[Path], targets: list[Path], *, force: bool) -> None:
  """Move/rename freshly-rendered PNGs to their target paths.

  When the target exists and `force` is False, the source is removed
  (treating the existing target as the source-of-truth cached output).
  """
  for src, dst in zip(rendered, targets):
    if src == dst:
      continue
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
      if force:
        dst.unlink()
      else:
        src.unlink(missing_ok=True)
        continue
    src.rename(dst)


def render_note_markdown(
  client: Client,
  file_id: str | int,
  output: str | os.PathLike | None = None,
  *,
  ocr_model: str = _ocr.DEFAULT_MODEL,
  ocr_engine: str = "supernote",
  force: bool = False,
  extra_prompt: str | None = None,
  on_page_start: Callable[[int, int], None] | None = None,
  on_token: Callable[[str], None] | None = None,
) -> str:
  """Build the stdout-equivalent markdown for a cloud `.note` file.

  When `output` is None, no PNGs persist. The text comes from the
  device transcript per page (or Ollama OCR if `ocr_engine="ollama"`,
  which renders to a tempdir, OCRs, and discards). Pages with no
  transcript render as `_(no transcript)_`.

  When `output` (a directory) is given, `page_{N}.png` files are
  written into it and the returned markdown includes per-page
  `![](output/page_N.png)` refs. With `ocr_engine="ollama"`,
  `content.md` is written into `output` as a cache marker.
  """
  use_ollama = ocr_engine == "ollama"

  if output is not None and use_ollama and not force:
    cached = Path(output) / "content.md"
    if cached.exists():
      return cached.read_text()

  pages: list[NotePage]
  image_refs: list[str] = []

  if output is None and not use_ollama:
    # Lightest path: just download .note, extract transcripts. No PNGs.
    with tempfile.NamedTemporaryFile(suffix=".note", delete=True) as tmp:
      download_file(client, file_id, Path(tmp.name))
      transcripts = extract_note_text(tmp.name)
    pages = [
      NotePage(
        index=i + 1,
        png_path=Path(""),
        transcript=t or None,
        ocr_text=None,
      )
      for i, t in enumerate(transcripts)
    ]
  elif output is None and use_ollama:
    # OCR path without persistence: render to tempdir, OCR, discard.
    with tempfile.TemporaryDirectory() as td:
      tdp = Path(td)
      pages = ocr_note_from_cloud(
        client, file_id, tdp,
        model=ocr_model, force=force, extra_prompt=extra_prompt,
        on_page_start=on_page_start, on_token=on_token,
      )
  else:
    # Output given: persist PNGs.
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    if use_ollama:
      pages = ocr_note_from_cloud(
        client, file_id, out,
        model=ocr_model, force=force, extra_prompt=extra_prompt,
        on_page_start=on_page_start, on_token=on_token,
      )
    else:
      with tempfile.NamedTemporaryFile(suffix=".note", delete=True) as tmp:
        download_file(client, file_id, Path(tmp.name))
        png_paths = render_note(tmp.name, out, force=force)
        transcripts = extract_note_text(tmp.name)
      pages = [
        NotePage(
          index=i + 1,
          png_path=png,
          transcript=(transcripts[i] if i < len(transcripts) else "") or None,
          ocr_text=None,
        )
        for i, png in enumerate(png_paths)
      ]
    prefix = str(output).rstrip("/") + "/"
    image_refs = [f"{prefix}{p.png_path.name}" for p in pages]

  md = _compose_note_markdown(pages, image_refs, empty_placeholder=NO_TRANSCRIPT_PLACEHOLDER)

  if output is not None and use_ollama:
    (Path(output) / "content.md").write_text(md)
  return md
