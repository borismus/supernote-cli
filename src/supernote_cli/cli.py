"""Thin argparse wrapper over supernote_cli.api."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
import time
from pathlib import Path

from . import api, ocr, tokenstore
from .client import ApiError, AuthRequired, Client


def _build_parser() -> argparse.ArgumentParser:
  p = argparse.ArgumentParser(prog="supernote", description="Supernote cloud CLI")
  p.add_argument("--no-cache", action="store_true", help="ignore and do not write the token cache")
  p.add_argument("--verbose", "-v", action="store_true", help="print HTTP calls")
  p.add_argument("--equipment-no", help="override SUPERNOTE_EQUIPMENT_NO")
  sub = p.add_subparsers(dest="cmd", required=True)

  sub.add_parser("login", help="authenticate and cache the token")
  sub.add_parser("logout", help="delete the cached token")
  sub.add_parser("whoami", help="show cached account and token age")

  ls = sub.add_parser("ls", help="list a folder's contents")
  ls.add_argument("path", nargs="?", default="", help="folder path, e.g. Note/Inbox")
  ls.add_argument("--json", dest="as_json", action="store_true")

  dl = sub.add_parser("download", help="download a file by path")
  dl.add_argument("path", nargs="?", help="remote file path, e.g. Note/Inbox/foo.note")
  dl.add_argument("--by-id", dest="by_id", help="download by file id instead of path")
  dl.add_argument("-o", "--output", help="local path (default: remote file name)")

  up = sub.add_parser("upload", help="upload a local file to a remote directory")
  up.add_argument("local", help="local file path")
  up.add_argument("remote_dir", help="existing remote directory, e.g. Document/Inbox")
  up.add_argument(
    "--overwrite",
    action="store_true",
    help="replace the remote file if it already exists",
  )

  rm = sub.add_parser("delete", help="delete a remote file by path")
  rm.add_argument("paths", nargs="*", help="remote file path(s) to delete")
  rm.add_argument("--by-id", dest="by_id", action="append", default=[], help="delete by file id (repeatable)")

  sy = sub.add_parser("sync", help="mirror a folder into a local directory")
  sy.add_argument("path", help="folder path to sync (e.g. Note)")
  sy.add_argument("-o", "--output", required=True, help="local output directory")
  sy.add_argument("--days-ago", dest="days_ago", type=int, help="only sync files modified within N days")
  sy.add_argument("--dry-run", action="store_true")
  sy.add_argument("--recursive", action="store_true")

  src = sub.add_parser(
    "source",
    help="source-document commands",
    description="Run `source ls` to list source documents that have digests.",
  )
  src.add_argument("target", help="'ls' (the only subcommand today)")
  src.add_argument("--days-ago", dest="days_ago", type=int)
  src.add_argument("--limit", type=int, default=50)
  src.add_argument("--json", dest="as_json", action="store_true")

  dg = sub.add_parser(
    "digest",
    help="digest (highlight + annotation) commands",
    description="Run `digest ls` to list, or `digest <id>[,id...]` to print JSON records.",
  )
  dg.add_argument("target", help="'ls' to list, or digest id(s) comma-separated")
  # ls
  dg.add_argument("--limit", type=int, default=20, help="max records to list")
  dg.add_argument("--days-ago", dest="days_ago", type=int, help="only include digests modified within N days")
  dg.add_argument("--json", dest="as_json", action="store_true", help="JSON output (ls only; id form is always JSON)")
  # <id>
  dg.add_argument("-o", "--output", default=".", help="output directory; PNGs go under {output}/attachments/")
  dg.add_argument("--no-ocr", dest="no_ocr", action="store_true", help="skip Ollama OCR of the handwritten annotation")
  dg.add_argument("--model", default=ocr.DEFAULT_MODEL, help=f"Ollama vision model (default: {ocr.DEFAULT_MODEL})")
  dg.add_argument("--force", action="store_true", help="overwrite existing handwritten_image PNGs")

  nt = sub.add_parser(
    "note",
    help=".note file commands",
    description="Run `note ls` to list, or `note <id>` to print a JSON per-page array.",
  )
  nt.add_argument("target", help="'ls' to list .note files, or a cloud file id to fetch one")
  # ls
  nt.add_argument("--limit", type=int, default=50, help="max records to list")
  nt.add_argument("--days-ago", dest="days_ago", type=int, help="only include files modified within N days")
  nt.add_argument("--json", dest="as_json", action="store_true", help="JSON output (ls only; id form is always JSON)")
  # <id>
  nt.add_argument("-o", "--output", help="output directory (default: ./note-{id}/); PNGs go under {output}/attachments/")
  nt.add_argument("--no-ocr", dest="no_ocr", action="store_true", help="skip Ollama OCR on each page")
  nt.add_argument("--model", default=ocr.DEFAULT_MODEL, help=f"Ollama vision model (default: {ocr.DEFAULT_MODEL})")
  nt.add_argument("--force", action="store_true", help="overwrite existing page PNGs")

  return p


def _client_from_args(args) -> Client:
  c = Client.from_env(no_cache=args.no_cache, verbose=args.verbose)
  if args.equipment_no:
    c.equipment_no = args.equipment_no
  return c


def _cmd_login(args) -> int:
  c = _client_from_args(args)
  c.token = None
  c.login()
  print(f"logged in as {c.account}, token cached at {tokenstore.token_path()}")
  return 0


def _cmd_logout(args) -> int:
  removed = tokenstore.clear()
  print("token cache cleared" if removed else "no token cache to remove")
  return 0


def _cmd_whoami(args) -> int:
  cached = tokenstore.load()
  if not cached:
    print("not logged in (no token cache)")
    return 1
  age = int(time.time() - cached.get("created_at", 0))
  hours, rem = divmod(age, 3600)
  mins = rem // 60
  tok = cached.get("token", "")
  print(f"account: {cached.get('account')}")
  print(f"token:   {tok[:12]}...{tok[-4:]}  (age: {hours}h{mins:02d}m)")
  return 0


def _cmd_ls(args) -> int:
  c = _client_from_args(args)
  _, contents = api.resolve_path(c, args.path)
  if args.as_json:
    print(json.dumps([_note_dict(n) for n in contents], indent=2))
    return 0
  for n in contents:
    kind = "d" if n.is_folder else "-"
    size = "" if n.is_folder else f"{n.size:>10}"
    mtime = n.update_time.strftime("%Y-%m-%d %H:%M")
    print(f"{kind} {size:>10}  {mtime}  {n.id:>20}  {n.file_name}")
  return 0


def _cmd_download(args) -> int:
  if not args.path and not args.by_id:
    print("error: provide a remote path or --by-id ID", file=sys.stderr)
    return 2
  if args.path and args.by_id:
    print("error: pass either a path or --by-id, not both", file=sys.stderr)
    return 2

  c = _client_from_args(args)
  if args.path:
    note = api.resolve_file(c, args.path)
    file_id = note.id
    default_name = note.file_name
  else:
    file_id = args.by_id
    default_name = f"supernote-{file_id}.note"

  dest = Path(args.output or default_name)
  n = c.download_to(api.download_url(c, file_id), dest)
  print(f"wrote {n} bytes to {dest}")
  return 0


def _cmd_upload(args) -> int:
  c = _client_from_args(args)
  note = api.upload_file(c, args.local, args.remote_dir, overwrite=args.overwrite)
  print(f"uploaded: {args.remote_dir.rstrip('/')}/{note.file_name}  (id={note.id}, {note.size} bytes)")
  return 0


def _cmd_delete(args) -> int:
  if not args.paths and not args.by_id:
    print("error: provide at least one path or --by-id ID", file=sys.stderr)
    return 2
  c = _client_from_args(args)
  rc = 0
  for path in args.paths:
    try:
      note = api.resolve_file(c, path)
      api.delete_file(c, note)
      print(f"deleted: {path}  (id={note.id})")
    except ApiError as e:
      print(f"error: {path}: {e}", file=sys.stderr)
      rc = 1
  for fid in args.by_id:
    # delete endpoint requires directoryId; walk the tree to find the file's parent.
    note = _find_note_by_id(c, fid)
    if note is None:
      print(f"error: id {fid}: not found", file=sys.stderr)
      rc = 1
      continue
    try:
      api.delete_file(c, note)
      print(f"deleted: id={fid}")
    except ApiError as e:
      print(f"error: id {fid}: {e}", file=sys.stderr)
      rc = 1
  return rc


def _find_note_by_id(client, file_id: str):
  """Walk the folder tree looking for a file with the given id. Escape hatch."""
  def _walk(directory_id):
    for n in api.list_files(client, directory_id):
      if str(n.id) == str(file_id) and not n.is_folder:
        return n
      if n.is_folder:
        found = _walk(n.id)
        if found is not None:
          return found
    return None
  return _walk(0)


def _cmd_sync(args) -> int:
  c = _client_from_args(args)
  entries = api.sync_folder(
    c, args.path, args.output,
    days_ago=args.days_ago, dry_run=args.dry_run, recursive=args.recursive,
  )
  counts: dict[str, int] = {}
  for e in entries:
    counts[e.action] = counts.get(e.action, 0) + 1
    if e.action == "download":
      prefix = "[DRY] would download" if args.dry_run else "downloaded"
      print(f"{prefix}: {e.note.file_name}  ({e.note.size} bytes)")
  print("")
  for k in ("download", "skip:uptodate", "skip:filter"):
    if k in counts:
      print(f"  {k}: {counts[k]}")
  return 0


def _cmd_source(args) -> int:
  if args.target != "ls":
    print(f"error: unknown source target '{args.target}'. Try `supernote source ls`.", file=sys.stderr)
    return 2
  c = _client_from_args(args)
  sources = api.list_digested_sources(c, days_ago=args.days_ago)
  sources = sources[: args.limit]
  if args.as_json:
    print(json.dumps([_source_summary_dict(s) for s in sources], indent=2))
    return 0
  if not sources:
    print("(no digested sources)")
    return 0
  for s in sources:
    print(f"{len(s.digests):>4}  {s.latest_modified.strftime('%Y-%m-%d')}  {s.source_path}")
  return 0


def _cmd_digest(args) -> int:
  if args.target == "ls":
    return _digest_ls(args)
  return _digest_show(args)


def _digest_ls(args) -> int:
  c = _client_from_args(args)
  hashes = api.fetch_digest_hashes(c, size=max(args.limit, 500))
  if args.days_ago is not None:
    cutoff = dt.datetime.now() - dt.timedelta(days=args.days_ago)
    hashes = [h for h in hashes if h.last_modified >= cutoff]
  hashes = hashes[: args.limit]
  if args.as_json:
    print(json.dumps([_digest_hash_dict(h) for h in hashes], indent=2))
    return 0
  if not hashes:
    print("(no digests)")
    return 0
  ids = [h.id for h in hashes]
  full = {d.id: d for d in api.fetch_digests_by_ids(c, ids)}
  for h in hashes:
    d = full.get(h.id)
    preview = ""
    src = ""
    if d:
      preview = (d.content or "").replace("\n", " ")[:80]
      src = d.source_path or ""
    print(f"{h.id:>20}  {src}  | {preview}")
  return 0


def _digest_show(args) -> int:
  c = _client_from_args(args)
  ids = [i.strip() for i in args.target.split(",") if i.strip()]
  out_dir = Path(args.output)
  attachments_dir = out_dir / "attachments"

  runner = _OcrRunner(enabled=not args.no_ocr, model=args.model)
  if runner.enabled:
    try:
      ocr.check_available()
    except ocr.OcrError as e:
      print(f"error: {e}", file=sys.stderr)
      return 2

  digests = api.fetch_digests_by_ids(c, ids)
  by_id = {d.id: d for d in digests}
  records = []
  for did in ids:
    d = by_id.get(did)
    if d is None:
      print(f"warning: digest {did} not found", file=sys.stderr)
      continue
    records.append(_digest_record(c, d, attachments_dir, out_dir, runner, force=args.force))

  print(json.dumps(records[0] if len(records) == 1 else records, indent=2))
  return 0


def _digest_record(c, digest, attachments_dir, out_dir, runner, *, force: bool) -> dict:
  rec: dict = {
    "id": digest.id,
    "digest": digest.content or "",
    "annotation": None,
    "handwritten_image": None,
    "source_path": digest.source_path,
    "last_modified": digest.last_modified_time.isoformat() if digest.last_modified_time else None,
  }
  if not digest.has_annotation:
    return rec
  png_paths = api.render_handwriting(c, digest, attachments_dir, force=force)
  if not png_paths:
    png_paths = _existing_handwriting_paths(attachments_dir, digest.id)
  rel_paths = [str(p.relative_to(out_dir)) if _is_relative_to(p, out_dir) else str(p) for p in png_paths]
  if rel_paths:
    rec["handwritten_image"] = rel_paths[0] if len(rel_paths) == 1 else rel_paths
  if runner.enabled and png_paths:
    ocr_texts = [runner.run(p) for p in png_paths]
    joined = "\n".join(t for t in ocr_texts if t)
    rec["annotation"] = joined or None
  return rec


def _existing_handwriting_paths(attachments_dir: Path, digest_id: str) -> list[Path]:
  single = attachments_dir / f"{digest_id}.png"
  if single.exists():
    return [single]
  found = []
  n = 0
  while (p := attachments_dir / f"{digest_id}_p{n + 1}.png").exists():
    found.append(p)
    n += 1
  return found


def _is_relative_to(p: Path, base: Path) -> bool:
  try:
    p.relative_to(base)
    return True
  except ValueError:
    return False


def _cmd_note(args) -> int:
  if args.target == "ls":
    return _note_ls(args)
  return _note_show(args)


def _note_ls(args) -> int:
  c = _client_from_args(args)
  pairs = api.list_notes(c, folder_path="Note", recursive=True)
  pairs.sort(key=lambda pn: pn[1].update_time, reverse=True)
  if args.days_ago is not None:
    cutoff = dt.datetime.now() - dt.timedelta(days=args.days_ago)
    pairs = [(fp, n) for fp, n in pairs if n.update_time >= cutoff]
  pairs = pairs[: args.limit]
  if args.as_json:
    print(json.dumps(
      [
        {"id": n.id, "folder_path": fp, "file_name": n.file_name, "size": n.size, "update_time": n.update_time.isoformat()}
        for fp, n in pairs
      ],
      indent=2,
    ))
    return 0
  if not pairs:
    print("(no .note files)")
    return 0
  for fp, n in pairs:
    mtime = n.update_time.strftime("%Y-%m-%d %H:%M")
    print(f"{n.size:>10}  {mtime}  {n.id:>20}  {fp}/{n.file_name}")
  return 0


def _note_show(args) -> int:
  c = _client_from_args(args)
  file_id = args.target
  out_dir = Path(args.output) if args.output else Path.cwd() / f"note-{file_id}"
  attachments_dir = out_dir / "attachments"
  attachments_dir.mkdir(parents=True, exist_ok=True)

  runner = _OcrRunner(enabled=not args.no_ocr, model=args.model)
  if runner.enabled:
    try:
      ocr.check_available()
    except ocr.OcrError as e:
      print(f"error: {e}", file=sys.stderr)
      return 2

  with tempfile.NamedTemporaryFile(suffix=".note", delete=True) as tmp:
    api.download_file(c, file_id, Path(tmp.name))
    png_paths = api.render_note(tmp.name, attachments_dir, force=args.force)
    transcripts = api.extract_note_text(tmp.name)

  # Rename page PNGs to the `{file_id}-p{N}.png` scheme for stable references.
  renamed: list[Path] = []
  for i, p in enumerate(png_paths):
    dest = attachments_dir / f"{file_id}-p{i + 1}.png"
    if p != dest:
      if dest.exists() and not args.force:
        p.unlink(missing_ok=True)
      else:
        p.replace(dest)
    renamed.append(dest)

  records = []
  for i, png in enumerate(renamed):
    rel = str(png.relative_to(out_dir)) if _is_relative_to(png, out_dir) else str(png)
    records.append({
      "page": i + 1,
      "transcript": (transcripts[i] if i < len(transcripts) else "") or None,
      "annotation": runner.run(png) if runner.enabled and png.exists() else None,
      "handwritten_image": rel,
    })
  print(json.dumps(records, indent=2))
  return 0


class _OcrRunner:
  """Runs `ocr_image` per path, giving up after the first error per invocation.

  On first error, prints the error to stderr and returns None for all
  subsequent calls. Callers should still respect `enabled` to skip entirely.
  """

  def __init__(self, *, enabled: bool, model: str):
    self.enabled = enabled
    self.model = model
    self._errored = False

  def run(self, path: Path) -> str | None:
    if not self.enabled or self._errored:
      return None
    try:
      return ocr.ocr_image(path, model=self.model)
    except ocr.OcrError as e:
      print(f"ollama error: {e}", file=sys.stderr)
      self._errored = True
      return None


def _note_dict(n) -> dict:
  return {
    "id": n.id,
    "directoryId": n.directory_id,
    "fileName": n.file_name,
    "size": n.size,
    "md5": n.md5,
    "isFolder": n.is_folder,
    "createTime": n.create_time.isoformat(),
    "updateTime": n.update_time.isoformat(),
  }


def _digest_hash_dict(h) -> dict:
  return {"id": h.id, "md5Hash": h.md5_hash, "lastModified": h.last_modified.isoformat()}


def _source_summary_dict(s) -> dict:
  return {
    "source_path": s.source_path,
    "source_stem": s.source_stem,
    "digest_count": len(s.digests),
    "latest_modified": s.latest_modified.isoformat(),
  }


_DISPATCH = {
  "login": _cmd_login,
  "logout": _cmd_logout,
  "whoami": _cmd_whoami,
  "ls": _cmd_ls,
  "download": _cmd_download,
  "upload": _cmd_upload,
  "delete": _cmd_delete,
  "sync": _cmd_sync,
  "digest": _cmd_digest,
  "source": _cmd_source,
  "note": _cmd_note,
}


def main(argv: list[str] | None = None) -> int:
  args = _build_parser().parse_args(argv)
  try:
    return _DISPATCH[args.cmd](args)
  except AuthRequired as e:
    print(f"error: {e}", file=sys.stderr)
    print(
      "set SUPERNOTE_USER and SUPERNOTE_PASSWORD in .env, then run: supernote login",
      file=sys.stderr,
    )
    return 2
  except ApiError as e:
    code = f" [{e.code}]" if e.code else ""
    print(f"error{code}: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  sys.exit(main())
