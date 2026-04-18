"""High-level API: endpoint wrappers + workflows over a Client."""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

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
    match = next(
      (n for n in contents if n.is_folder and n.file_name == part), None
    )
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


def fetch_handwriting_url(client: Client, digest_id: str | int) -> str | None:
  """Ask the API for a signed URL to the per-highlight handwriting .mark file.

  Returns None if the digest has no associated handwriting (the highlight
  was never annotated). Other errors raise.
  """
  data = client._post(
    "file/download/summary", {"id": digest_id}, include_channel=True
  )
  return data.get("url")


def render_handwriting(
  client: Client,
  digest: Digest,
  out_dir: str | os.PathLike = ".",
  *,
  force: bool = False,
) -> list[Path]:
  """Fetch the per-highlight handwriting for a digest and render each page to PNG.

  Returns the list of written PNG paths. Empty list if:
    - the digest has no commentHandwriteName (highlight with no annotation), or
    - every target PNG already exists and force is False.

  Files are named `{digest_id}.png` for single-page handwriting, or
  `{digest_id}_p{N}.png` (1-indexed) for multi-page. Existing files are
  skipped unless `force=True`.
  """
  from supernotelib import load_notebook
  from supernotelib.converter import ImageConverter

  if not (digest.raw.get("commentHandwriteName") or ""):
    return []
  url = fetch_handwriting_url(client, digest.id)
  if not url:
    return []

  out = Path(out_dir)
  out.mkdir(parents=True, exist_ok=True)
  mark_bytes = client.get_binary(url)

  with tempfile.NamedTemporaryFile(suffix=".mark") as tmp:
    tmp.write(mark_bytes)
    tmp.flush()
    notebook = load_notebook(tmp.name)
    total = notebook.get_total_pages()
    converter = ImageConverter(notebook, palette=None)

    written: list[Path] = []
    for i in range(total):
      suffix = "" if total == 1 else f"_p{i + 1}"
      dest = out / f"{digest.id}{suffix}.png"
      if dest.exists() and not force:
        continue
      img = converter.convert(i)
      if img is None:
        continue
      img.save(dest)
      written.append(dest)
    return written
