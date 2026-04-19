"""High-level API: endpoint wrappers + workflows over a Client."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
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
  out: str | os.PathLike = ".",
  *,
  force: bool = False,
) -> list[Path]:
  """Fetch the per-highlight handwriting for a digest and render each page to PNG.

  `out` may be either:
    - a directory (default) — files are named `{digest_id}.png`, or
      `{digest_id}_p{N}.png` (1-indexed) for multi-page
    - a `.png` file path — used directly for single-page handwriting;
      for multi-page, siblings `{stem}_p{N}.png` are written next to it

  Returns the list of written PNG paths. Empty list if:
    - the digest has no commentHandwriteName (highlight with no annotation), or
    - every target PNG already exists and force is False.

  Existing files are skipped unless `force=True`.
  """
  from supernotelib import load_notebook
  from supernotelib.converter import ImageConverter

  if not (digest.raw.get("commentHandwriteName") or ""):
    return []
  url = fetch_handwriting_url(client, digest.id)
  if not url:
    return []

  out_path = Path(out)
  as_file = out_path.suffix.lower() == ".png"
  mark_bytes = client.get_binary(url)

  with tempfile.NamedTemporaryFile(suffix=".mark") as tmp:
    tmp.write(mark_bytes)
    tmp.flush()
    notebook = load_notebook(tmp.name)
    total = notebook.get_total_pages()
    converter = ImageConverter(notebook, palette=None)

    written: list[Path] = []
    for i in range(total):
      if as_file:
        if total == 1:
          dest = out_path
        else:
          dest = out_path.with_name(f"{out_path.stem}_p{i + 1}{out_path.suffix}")
      else:
        suffix = "" if total == 1 else f"_p{i + 1}"
        dest = out_path / f"{digest.id}{suffix}.png"
      dest.parent.mkdir(parents=True, exist_ok=True)
      if dest.exists() and not force:
        continue
      img = converter.convert(i)
      if img is None:
        continue
      img.save(dest)
      written.append(dest)
    return written


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
    ocr_text = _ocr.ocr_image(png, model=model) if png.exists() else None
    pages.append(
      NotePage(
        index=i + 1,
        png_path=png,
        transcript=transcript or None,
        ocr_text=ocr_text,
      )
    )
  return pages
