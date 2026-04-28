"""High-level API: endpoint wrappers + workflows over a Client."""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ocr as _ocr
from .client import ApiError, Client
from .models import Digest, DigestHash, Note


@contextlib.contextmanager
def _workdir(dir: str | os.PathLike | None):
  """Yield a Path that is either a TemporaryDirectory or the given dir.

  When dir is None, a tempdir is created and cleaned up on exit. Otherwise
  the given dir is created if needed and left in place.
  """
  if dir is None:
    with tempfile.TemporaryDirectory() as td:
      yield Path(td)
  else:
    p = Path(dir)
    p.mkdir(parents=True, exist_ok=True)
    yield p


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
  """Render a digest's handwriting to `page_N.png` (1-indexed) inside `dir`.

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
      dest = out / f"page_{i + 1}.png"
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
) -> list[NotePage]:
  """Render a `.note` file's pages, pull device transcripts, OCR each page.

  Bundles `render_note` + `extract_note_text` + `ocr_image` into one call.
  If Ollama is unreachable, `ocr_text` is None on every page but the rest
  of the record is still populated.
  """
  png_paths = render_note(note_path, out_dir, force=force)
  transcripts = extract_note_text(note_path)

  pages: list[NotePage] = []
  for i, png in enumerate(png_paths):
    transcript = transcripts[i] if i < len(transcripts) else ""
    ocr_text: str | None = None
    if png.exists():
      try:
        ocr_text = _ocr.ocr_image(png, model=model)
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
) -> list[NotePage]:
  """Download a cloud `.note` by id to a temp file, then run `ocr_note`.

  PNG outputs persist under `out_dir`; the downloaded `.note` is discarded
  after rendering.
  """
  with tempfile.NamedTemporaryFile(suffix=".note", delete=True) as tmp:
    download_file(client, file_id, Path(tmp.name))
    return ocr_note(tmp.name, out_dir, model=model, force=force)


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


def _compose_digest_markdown(highlight: str, ocr_body: str) -> str:
  """Build digest markdown: blockquoted highlight + optional OCR body."""
  if highlight:
    quoted = "\n".join(f"> {line}" for line in highlight.splitlines())
  else:
    quoted = "> "
  if ocr_body:
    return f"{quoted}\n\n{ocr_body.rstrip()}\n"
  return f"{quoted}\n"


def _compose_note_markdown(pages: list[NotePage]) -> str:
  """Build note markdown: one ## Page N section per page."""
  sections = []
  for p in pages:
    text = (p.ocr_text or "").rstrip()
    sections.append(f"## Page {p.index}\n\n{text}\n")
  return "\n".join(sections) if sections else ""


_NOTE_PAGE_HEADER_RE = re.compile(r"^## Page (\d+)\s*$", re.MULTILINE)


def _parse_digest_markdown(md: str) -> tuple[str, str | None]:
  """Inverse of _compose_digest_markdown.

  Returns (highlight, ocr_text_or_None). The highlight is the joined
  content of leading `> `-prefixed lines (newlines preserved); the OCR
  body is everything after the first blank line that follows the
  blockquote, stripped. Returns ocr=None when no body is present.
  """
  lines = md.splitlines()
  quote_lines: list[str] = []
  i = 0
  while i < len(lines) and lines[i].startswith(">"):
    quote_lines.append(lines[i][1:].lstrip(" "))
    i += 1
  highlight = "\n".join(quote_lines)
  # Skip blank separator lines.
  while i < len(lines) and lines[i].strip() == "":
    i += 1
  body = "\n".join(lines[i:]).strip()
  return highlight, (body or None)


def _parse_note_markdown(md: str) -> list[tuple[int, str]]:
  """Inverse of _compose_note_markdown.

  Returns [(page_number, ocr_text), ...] in order of appearance.
  """
  matches = list(_NOTE_PAGE_HEADER_RE.finditer(md))
  out: list[tuple[int, str]] = []
  for idx, m in enumerate(matches):
    page = int(m.group(1))
    body_start = m.end()
    body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
    body = md[body_start:body_end].strip("\n")
    # Drop the single blank separator after the header.
    if body.startswith("\n"):
      body = body[1:]
    out.append((page, body.rstrip()))
  return out


def render_digest_markdown(
  client: Client,
  digest: Digest,
  *,
  ocr_model: str = _ocr.DEFAULT_MODEL,
  no_ocr: bool = False,
  force: bool = False,
  dir: str | os.PathLike | None = None,
) -> str:
  """Build the stdout-equivalent markdown for a digest.

  Format: blockquoted highlight + (when handwriting exists and `no_ocr` is
  False) the OCR text below, separated by a blank line.

  When `dir` is given, `page_N.png` and `content.md` are persisted there;
  on re-run, a cached `content.md` is returned unless `force=True`. When
  `dir` is None, work happens in a tempdir that's discarded.
  """
  if dir is not None and not force:
    cached = Path(dir) / "content.md"
    if cached.exists():
      return cached.read_text()

  ocr_body = ""
  # Render PNGs whenever there's handwriting AND either --dir wants
  # persistence or we'll OCR them. Skip work entirely if neither applies.
  if digest.has_annotation and (dir is not None or not no_ocr):
    with _workdir(dir) as work:
      pages = render_handwriting(client, digest, work, force=force)
      if not no_ocr:
        parts = [_ocr.ocr_image(p, model=ocr_model) or "" for p in pages]
        ocr_body = "\n\n".join(part for part in parts if part)

  md = _compose_digest_markdown(digest.content or "", ocr_body)

  if dir is not None:
    out = Path(dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "content.md").write_text(md)
  return md


def render_note_markdown(
  client: Client,
  file_id: str | int,
  *,
  ocr_model: str = _ocr.DEFAULT_MODEL,
  no_ocr: bool = False,
  force: bool = False,
  dir: str | os.PathLike | None = None,
) -> str:
  """Build the stdout-equivalent markdown for a cloud `.note` file.

  Format: `## Page N\\n\\n{ocr text}\\n` per page, concatenated.

  When `dir` is given, `page_N.png` + `content.md` are persisted there;
  on re-run, the cached `content.md` is returned unless `force=True`.
  """
  if dir is not None and not force:
    cached = Path(dir) / "content.md"
    if cached.exists():
      return cached.read_text()

  with _workdir(dir) as work:
    if no_ocr:
      # Render PNGs only (no OCR call); build NotePage list with empty ocr_text.
      with tempfile.NamedTemporaryFile(suffix=".note", delete=True) as tmp:
        download_file(client, file_id, Path(tmp.name))
        png_paths = render_note(tmp.name, work, force=force)
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
    else:
      pages = ocr_note_from_cloud(client, file_id, work, model=ocr_model, force=force)

  md = _compose_note_markdown(pages)

  if dir is not None:
    out = Path(dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "content.md").write_text(md)
  return md
