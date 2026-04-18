"""Thin argparse wrapper over supernote_cli.api."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import api, tokenstore
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

  dl = sub.add_parser("download", help="download a single file by id")
  dl.add_argument("file_id")
  dl.add_argument("-o", "--output", help="local path (default: file name from API)")

  sy = sub.add_parser("sync", help="mirror a folder into a local directory")
  sy.add_argument("path", help="folder path to sync (e.g. Note)")
  sy.add_argument("--out", required=True, help="local output directory")
  sy.add_argument("--days-ago", type=int, help="only sync files modified within N days")
  sy.add_argument("--dry-run", action="store_true")
  sy.add_argument("--recursive", action="store_true")

  pd = sub.add_parser(
    "digest",
    help="list digests (`digest ls`) or render one as PNG (`digest <id>`)",
    description=(
      "With target 'ls': list digest summary records. "
      "With one or more comma-separated digest IDs: print content and render "
      "the handwritten annotation (the note drawn on top of the highlight) "
      "to PNG named {digest_id}.png in the output directory."
    ),
  )
  pd.add_argument(
    "target",
    help="'ls' to list, or digest ID(s) (comma-separated) to render",
  )
  pd.add_argument("--json", dest="as_json", action="store_true")
  # ls-only
  pd.add_argument(
    "--limit", type=int, default=20, help="(ls only) max records to show"
  )
  # render-only
  pd.add_argument(
    "-o",
    "--out",
    default=".",
    help="(render only) directory to save rendered PNG into (default: CWD)",
  )
  pd.add_argument(
    "--no-annotation",
    dest="no_annotation",
    action="store_true",
    help="(render only) print digest content only; skip the PNG render",
  )
  pd.add_argument(
    "--force",
    action="store_true",
    help="(render only) overwrite existing PNG files",
  )

  return p


def _client_from_args(args) -> Client:
  c = Client.from_env(no_cache=args.no_cache, verbose=args.verbose)
  if args.equipment_no:
    c.equipment_no = args.equipment_no
  return c


def _cmd_login(args) -> int:
  c = _client_from_args(args)
  c.token = None  # force fresh login even if cache exists
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
  c = _client_from_args(args)
  out = args.output
  if out is None:
    out = f"supernote-{args.file_id}.note"
  dest = Path(out)
  n = c.download_to(api.download_url(c, args.file_id), dest)
  print(f"wrote {n} bytes to {dest}")
  return 0


def _cmd_sync(args) -> int:
  c = _client_from_args(args)
  entries = api.sync_folder(
    c,
    args.path,
    args.out,
    days_ago=args.days_ago,
    dry_run=args.dry_run,
    recursive=args.recursive,
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


def _cmd_digest(args) -> int:
  if args.target == "ls":
    return _digest_list_impl(args)
  return _digest_render_impl(args)


def _digest_list_impl(args) -> int:
  c = _client_from_args(args)
  hashes = api.fetch_digest_hashes(c, size=max(args.limit, 500))
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


def _digest_render_impl(args) -> int:
  c = _client_from_args(args)
  ids = [i.strip() for i in args.target.split(",") if i.strip()]
  digests = api.fetch_digests_by_ids(c, ids)

  if args.as_json:
    print(json.dumps([_digest_dict(d) for d in digests], indent=2))
  else:
    for d in digests:
      print(f"=== {d.id}  {d.source_path or ''}")
      print(d.content or "(empty)")
      print()

  if args.no_annotation:
    return 0

  out_dir = Path(args.out)
  out_dir.mkdir(parents=True, exist_ok=True)
  for d in digests:
    written = api.render_handwriting(c, d, out_dir, force=args.force)
    if written:
      for p in written:
        print(f"rendered: {p}", file=sys.stderr)
      continue
    # Empty list: either already-existed-and-not-forced, or no annotation
    if d.raw.get("commentHandwriteName"):
      # Had annotation; must have skipped because files exist
      total = _expected_page_count(out_dir, d.id)
      if total:
        for i in range(1, total + 1):
          suffix = "" if total == 1 else f"_p{i}"
          print(f"already exists: {out_dir / f'{d.id}{suffix}.png'}", file=sys.stderr)
      else:
        print(f"already exists: {out_dir / f'{d.id}.png'}", file=sys.stderr)
    else:
      print(f"no annotation for {d.id}", file=sys.stderr)
  return 0


def _expected_page_count(out_dir: Path, digest_id: str) -> int:
  """How many PNG pages are already on disk for this digest."""
  single = out_dir / f"{digest_id}.png"
  if single.exists():
    return 1
  n = 0
  while (out_dir / f"{digest_id}_p{n + 1}.png").exists():
    n += 1
  return n


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


def _digest_dict(d) -> dict:
  return {"id": d.id, "sourcePath": d.source_path, "content": d.content}


_DISPATCH = {
  "login": _cmd_login,
  "logout": _cmd_logout,
  "whoami": _cmd_whoami,
  "ls": _cmd_ls,
  "download": _cmd_download,
  "sync": _cmd_sync,
  "digest": _cmd_digest,
}


def main(argv: list[str] | None = None) -> int:
  args = _build_parser().parse_args(argv)
  try:
    return _DISPATCH[args.cmd](args)
  except AuthRequired as e:
    print(f"error: {e}", file=sys.stderr)
    print("set SUPERNOTE_USER and SUPERNOTE_PASSWORD in .env, then run: supernote login", file=sys.stderr)
    return 2
  except ApiError as e:
    code = f" [{e.code}]" if e.code else ""
    print(f"error{code}: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  sys.exit(main())
