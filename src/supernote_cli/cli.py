"""Thin argparse wrapper over supernote_cli.api."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import api, ocr, tokenstore
from .client import ApiError, AuthRequired, Client


def _ls_format_time(d: dt.datetime, now: dt.datetime | None = None) -> str:
  """Format mtime like macOS `ls -l`: `Mon DD HH:MM` when within ~6 months,
  `Mon DD  YYYY` otherwise. Uses local time (the api's datetime objects come
  from `datetime.fromtimestamp(...)` so they're already in local tz)."""
  if now is None:
    now = dt.datetime.now()
  if abs((now - d).days) < 180:
    return d.strftime("%b %e %H:%M")
  return d.strftime("%b %e  %Y")


class _Marquee:
  """Two-line OCR progress display on stderr:

      Transcribing {spinner} Page N/M
      Thinking: {marquee tail filling the rest of the terminal width}

  A daemon thread re-renders every TICK_SECONDS so the spinner animates
  even when no tokens are arriving. Lines are redrawn in place via ANSI
  cursor-up + clear, so the actual OCR output written to stdout after
  `close()` lands on a clean line.

  When stderr is not a TTY (piped/redirected) we drop ANSI control codes
  entirely and emit one `[page N/M] OCR...` log line per page boundary.

  Callbacks the api layer fires:
    `marquee.page(index, total)` — called once per page before OCR starts.
    `marquee.token(delta)` — called per Ollama streaming chunk.

  Always call `marquee.close()` to stop the thread and clear both lines.
  """

  SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
  TICK_SECONDS = 0.1
  THINKING_PREFIX = "Thinking: "

  def __init__(self):
    self._tty = sys.stderr.isatty()
    self._buf = ""
    self._lock = threading.Lock()
    self._stop = threading.Event()
    self._thread: threading.Thread | None = None
    self._page_idx = 0
    self._page_total = 0
    self._spin_i = 0
    self._drawn = False  # are line1+line2 currently on screen?

  def page(self, idx: int, total: int) -> None:
    if self._tty:
      self._clear_lines()
    self._page_idx = idx
    self._page_total = total
    with self._lock:
      self._buf = ""
    if not self._tty:
      sys.stderr.write(f"[page {idx}/{total}] OCR...\n")
      sys.stderr.flush()
      return
    if self._thread is None:
      self._thread = threading.Thread(target=self._tick, daemon=True)
      self._thread.start()
    self._render()

  def token(self, text: str) -> None:
    if not text:
      return
    with self._lock:
      self._buf += text

  def close(self) -> None:
    self._stop.set()
    if self._thread is not None:
      self._thread.join(timeout=0.5)
      self._thread = None
    if self._tty:
      self._clear_lines()

  # internals

  def _tick(self) -> None:
    while not self._stop.wait(self.TICK_SECONDS):
      self._render()

  def _term_cols(self) -> int:
    return max(20, shutil.get_terminal_size((80, 20)).columns)

  def _render(self) -> None:
    if not self._tty or self._page_idx == 0:
      return
    cols = self._term_cols()
    spin = self.SPINNER[self._spin_i % len(self.SPINNER)]
    self._spin_i += 1
    line1 = f"Transcribing {spin} Page {self._page_idx}/{self._page_total}"
    width = max(1, cols - len(self.THINKING_PREFIX))
    with self._lock:
      tail = self._buf.replace("\n", " ").replace("\r", " ")[-width:]
    line2 = f"{self.THINKING_PREFIX}{tail}"
    line1 = line1[:cols].ljust(cols)
    line2 = line2[:cols].ljust(cols)
    if self._drawn:
      # Move cursor to start of line1, clear, write, then line2.
      out = f"\r\x1b[1A\x1b[2K{line1}\n\x1b[2K{line2}"
    else:
      out = f"{line1}\n{line2}"
      self._drawn = True
    sys.stderr.write(out)
    sys.stderr.flush()

  def _clear_lines(self) -> None:
    """Erase the two rendered lines and leave the cursor at the start of
    the (now-blank) first line, so subsequent stdout output starts there."""
    if not self._drawn:
      return
    sys.stderr.write("\r\x1b[2K\x1b[1A\x1b[2K")
    sys.stderr.flush()
    self._drawn = False


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
    description="Run `source ls` to list source documents that have annotations.",
  )
  src.add_argument("target", help="'ls' (the only subcommand today)")
  src.add_argument("--days-ago", dest="days_ago", type=int)
  src.add_argument("--limit", type=int, default=50)
  src.add_argument("--json", dest="as_json", action="store_true")

  an = sub.add_parser(
    "annotation",
    aliases=("an",),
    help="annotation (highlight + handwriting) commands",
    description=(
      "Run `annotation ls` to list annotation records. With an ID, "
      "print the blockquoted highlight to stdout. Pass `--ocr` to also "
      "transcribe the handwriting via local Ollama vision OCR. Pass "
      "`-o PATH` to persist the handwriting PNG (and, with `--ocr`, "
      "a cache markdown) alongside."
    ),
  )
  an.add_argument("target", help="'ls' to list, or annotation id(s) comma-separated")
  # ls
  an.add_argument("--limit", type=int, default=20, help="(ls only) max records to list")
  an.add_argument("--days-ago", dest="days_ago", type=int, help="(ls only) only include annotations modified within N days")
  # both
  an.add_argument(
    "--json",
    dest="as_json",
    action="store_true",
    help="emit JSON instead of markdown (id form) / table (ls form)",
  )
  # <id>
  an.add_argument(
    "-o", "--output",
    dest="output",
    default=None,
    help=(
      "(id form) write the handwriting PNG and a cache markdown alongside. "
      "Path ending in .png: file mode (single PNG at PATH; multi-page fans "
      "to {stem}_pN.png; cache md at PATH with .md suffix). Otherwise: dir "
      "mode (PNG at PATH/{annotation_id}.png; cache md at PATH/{annotation_id}.md). "
      "Default: no files persisted."
    ),
  )
  an.add_argument(
    "--ocr",
    dest="ocr",
    action="store_true",
    help=(
      "(id form) transcribe the handwriting via local Ollama vision OCR. "
      "Default off — markdown body has just the blockquote."
    ),
  )
  an.add_argument("--model", default=ocr.DEFAULT_MODEL, help=f"(id form, with --ocr) Ollama vision model (default: {ocr.DEFAULT_MODEL})")
  an.add_argument("--force", action="store_true", help="(id form) re-render PNGs and re-OCR even if cached on disk")
  an.add_argument("--prompt", dest="prompt", help="(id form, with --ocr) extra OCR instructions appended to the default prompt")

  nb = sub.add_parser(
    "notebook",
    aliases=("nb",),
    help="notebook (.note file) commands",
    description=(
      "Run `notebook ls` to list .note files under /Note/. With a numeric "
      "file id, print the device-OCR transcript per page. Pass `-o DIR` to "
      "also persist `page_N.png`; pass `--ocr ollama` to swap the device "
      "transcript for Ollama vision OCR."
    ),
  )
  nb.add_argument("target", help="'ls' to list notebooks, or a target to fetch: numeric file id (recommended), basename (foo.note or foo), or full path (Note/sub/foo.note)")
  # ls
  nb.add_argument("--limit", type=int, default=50, help="(ls only) max records to list")
  nb.add_argument("--days-ago", dest="days_ago", type=int, help="(ls only) only include files modified within N days")
  # both
  nb.add_argument(
    "--json",
    dest="as_json",
    action="store_true",
    help="emit JSON instead of markdown (id form) / table (ls form)",
  )
  # <id>
  nb.add_argument(
    "-o", "--output",
    dest="output",
    default=None,
    help="(id form) directory to write page_N.png (and content.md when --ocr ollama). Default: no PNGs persisted, transcripts only.",
  )
  nb.add_argument(
    "--ocr",
    dest="ocr",
    choices=("supernote", "ollama"),
    default="supernote",
    help="(id form) handwriting transcription engine. 'supernote' (default): use the device's on-tablet OCR transcript per page (pages with no transcript show '_(no transcript)_'). 'ollama': run local Ollama vision OCR per page.",
  )
  nb.add_argument("--model", default=ocr.DEFAULT_MODEL, help=f"(id form, with --ocr ollama) Ollama vision model (default: {ocr.DEFAULT_MODEL})")
  nb.add_argument("--force", action="store_true", help="(id form) re-render PNGs and re-OCR even if cached on disk")
  nb.add_argument("--prompt", dest="prompt", help="(id form, with --ocr ollama) extra OCR instructions appended to the default prompt")

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
    mtime = _ls_format_time(n.update_time)
    print(f" {kind} {size:>10}  {mtime}  {n.id}  {n.file_name}")
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
    print("(no sources with annotations)")
    return 0
  for s in sources:
    print(f" {len(s.digests):>4}  {_ls_format_time(s.latest_modified)}  {s.source_path}")
  return 0


def _cmd_annotation(args) -> int:
  if args.target == "ls":
    return _annotation_ls(args)
  return _annotation_show(args)


def _annotation_ls(args) -> int:
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
    print("(no annotations)")
    return 0
  ids = [h.id for h in hashes]
  full = {d.id: d for d in api.fetch_digests_by_ids(c, ids)}

  # Group by source so the source filename only prints once per group.
  by_src: dict[str, list[tuple]] = {}
  for h in hashes:
    d = full.get(h.id)
    if d is None:
      continue
    src = d.source_path or ""
    by_src.setdefault(src, []).append((h, d))

  # Sources sorted by most-recent activity, oldest-first so the freshest
  # source group lands at the bottom (latest entry just above the prompt —
  # matches notebook ls).
  src_order = sorted(
    by_src,
    key=lambda s: max(h.last_modified for h, _ in by_src[s]),
  )

  cols, _ = shutil.get_terminal_size((80, 20))
  # ` (A)` is appended on rows whose digest has handwriting on top, so the
  # eye can scan for which entries have an extra layer to pull. Reserve the
  # 4-char width on every row so the marker (when present) always lands at
  # the same column.
  marker_w = 4
  for i, src in enumerate(src_order):
    if i > 0:
      print()
    doc = src.rsplit("/", 1)[-1] if src else "(unknown source)"
    print(doc)
    items = sorted(by_src[src], key=lambda hd: hd[0].last_modified)
    for h, d in items:
      mtime = _ls_format_time(h.last_modified)
      prefix = f" {h.id}  {mtime}  "
      # Reserve marker_w for the trailing marker + 1 col for right margin.
      available = max(20, cols - len(prefix) - marker_w - 1)
      preview = (d.content or "").replace("\n", " ")[:available]
      marker = " (A)" if d.has_annotation else ""
      print(f"{prefix}{preview.ljust(available)}{marker}")
  return 0


def _annotation_show(args) -> int:
  c = _client_from_args(args)
  ids = [i.strip() for i in args.target.split(",") if i.strip()]
  use_ollama = bool(args.ocr)
  ocr_engine = "ollama" if use_ollama else "none"

  if args.output is not None and len(ids) > 1:
    is_file_mode = args.output.lower().endswith(".png")
    if is_file_mode:
      print(
        "error: -o file.png is single-id; pass a single annotation id or use a directory",
        file=sys.stderr,
      )
      return 2
    if use_ollama:
      print(
        "error: multi-id with -o --ocr is unsupported; pass a single annotation id",
        file=sys.stderr,
      )
      return 2

  if args.prompt and not use_ollama:
    print("warning: --prompt has no effect without --ocr", file=sys.stderr)

  if use_ollama:
    try:
      ocr.check_available()
    except ocr.OcrError as e:
      print(f"error: {e}", file=sys.stderr)
      return 2

  digests = api.fetch_digests_by_ids(c, ids)
  by_id = {d.id: d for d in digests}

  if args.as_json:
    records = []
    for did in ids:
      d = by_id.get(did)
      if d is None:
        print(f"warning: annotation {did} not found", file=sys.stderr)
        continue
      records.append(_annotation_json_record(c, d, args))
    print(json.dumps(records[0] if len(records) == 1 else records, indent=2))
    return 0

  # Markdown path.
  marquee = _Marquee()
  try:
    for i, did in enumerate(ids):
      d = by_id.get(did)
      if d is None:
        print(f"warning: annotation {did} not found", file=sys.stderr)
        continue
      md = api.render_digest_markdown(
        c, d, args.output,
        ocr_model=args.model,
        ocr_engine=ocr_engine,
        force=args.force,
        extra_prompt=args.prompt,
        on_page_start=marquee.page,
        on_token=marquee.token,
      )
      if i > 0:
        sys.stdout.write("\n")
      sys.stdout.write(md)
      if not md.endswith("\n"):
        sys.stdout.write("\n")
      # Surface untranscribed-digest hint when nothing pulls it.
      if d.has_annotation and args.output is None and not use_ollama:
        print(
          f"\nNote: annotation {d.id} has untranscribed digest; "
          "pass --ocr to transcribe (or -o PATH to save the PNG)",
          file=sys.stderr,
        )
  finally:
    marquee.close()
  return 0


def _annotation_json_record(c, digest, args) -> dict:
  """Build a v0.2-shaped JSON record for a digest."""
  marquee = _Marquee()
  try:
    md = api.render_digest_markdown(
      c, digest, args.output,
      ocr_model=args.model,
      ocr_engine="ollama" if args.ocr else "none",
      force=args.force,
      extra_prompt=args.prompt,
      on_page_start=marquee.page,
      on_token=marquee.token,
    )
  finally:
    marquee.close()
  _, annotation = api._parse_digest_markdown(md)

  rec: dict = {
    "id": digest.id,
    "digest": digest.content or "",
    "annotation": annotation,
    "handwritten_image": None,
    "source_path": digest.source_path,
    "last_modified": digest.last_modified_time.isoformat() if digest.last_modified_time else None,
  }

  if digest.has_annotation and args.output is not None:
    rels = _list_annotation_image_refs(args.output, digest.id)
    if rels:
      rec["handwritten_image"] = rels[0] if len(rels) == 1 else rels
  return rec


def _list_annotation_image_refs(output: str, digest_id: str) -> list[str]:
  """Enumerate the on-disk PNGs the digest produced under `-o`, returning
  refs as they should appear in the markdown / JSON output."""
  is_file = output.lower().endswith(".png")
  p = Path(output)
  if is_file:
    if p.exists():
      return [output]
    parent = p.parent
    stem = p.stem
    refs = []
    n = 1
    ref_parent = output[: -len(p.name)]
    while (parent / f"{stem}_p{n}.png").exists():
      refs.append(f"{ref_parent}{stem}_p{n}.png")
      n += 1
    return refs
  prefix = output.rstrip("/") + "/"
  single = p / f"{digest_id}.png"
  if single.exists():
    return [f"{prefix}{single.name}"]
  refs = []
  n = 1
  while (p / f"{digest_id}_p{n}.png").exists():
    refs.append(f"{prefix}{digest_id}_p{n}.png")
    n += 1
  return refs


def _list_page_pngs(dir: Path) -> list[str]:
  """Enumerate `page_N.png` filenames in `dir`, sorted by N."""
  out = []
  n = 1
  while (dir / f"page_{n}.png").exists():
    out.append(f"page_{n}.png")
    n += 1
  return out


def _cmd_notebook(args) -> int:
  if args.target == "ls":
    return _notebook_ls(args)
  return _notebook_show(args)


def _notebook_ls(args) -> int:
  c = _client_from_args(args)
  pairs = api.list_notes(c, folder_path="Note", recursive=True)
  if args.days_ago is not None:
    cutoff = dt.datetime.now() - dt.timedelta(days=args.days_ago)
    pairs = [(fp, n) for fp, n in pairs if n.update_time >= cutoff]
  # Sort newest-first to apply --limit, then reverse so most recent is last
  # (natural for a terminal — the latest entry is right above your prompt).
  pairs.sort(key=lambda pn: pn[1].update_time, reverse=True)
  pairs = pairs[: args.limit]
  pairs.reverse()
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
  basenames = [n.file_name for _, n in pairs]
  collisions = {b for b in basenames if basenames.count(b) > 1}
  for fp, n in pairs:
    mtime = _ls_format_time(n.update_time)
    name = n.file_name[:-5] if n.file_name.endswith(".note") else n.file_name
    if n.file_name in collisions:
      rel = fp[len("Note"):].lstrip("/")
      label = f"{rel}/{name}" if rel else name
    else:
      label = name
    print(f" {n.id}  {mtime}  {label}")
  return 0


def _notebook_show(args) -> int:
  c = _client_from_args(args)
  try:
    note = api.resolve_note(c, args.target)
  except (api.NoteNotFound, api.NoteAmbiguous) as e:
    print(f"error: {e}", file=sys.stderr)
    return 2
  file_id = note.id
  use_ollama = args.ocr == "ollama"

  if args.prompt and not use_ollama:
    print("warning: --prompt has no effect without --ocr ollama", file=sys.stderr)

  if use_ollama:
    try:
      ocr.check_available()
    except ocr.OcrError as e:
      print(f"error: {e}", file=sys.stderr)
      return 2

  if args.as_json:
    rec = _notebook_json_record(c, file_id, args)
    print(json.dumps(rec, indent=2))
    return 0

  marquee = _Marquee()
  try:
    md = api.render_note_markdown(
      c, file_id, args.output,
      ocr_model=args.model,
      ocr_engine=args.ocr,
      force=args.force,
      extra_prompt=args.prompt,
      on_page_start=marquee.page,
      on_token=marquee.token,
    )
  finally:
    marquee.close()
  sys.stdout.write(md)
  if not md.endswith("\n"):
    sys.stdout.write("\n")
  return 0


def _notebook_json_record(c, file_id, args) -> list[dict]:
  """Build the v0.2-shaped per-page JSON for a .note."""
  marquee = _Marquee()
  try:
    md = api.render_note_markdown(
      c, file_id, args.output,
      ocr_model=args.model,
      ocr_engine=args.ocr,
      force=args.force,
      extra_prompt=args.prompt,
      on_page_start=marquee.page,
      on_token=marquee.token,
    )
  finally:
    marquee.close()

  page_ocr = dict(api._parse_note_markdown(md))
  # Device transcripts aren't cached on disk; re-fetch via supernotelib by
  # downloading the .note again. Fast enough for the JSON path.
  with tempfile.NamedTemporaryFile(suffix=".note", delete=True) as tmp:
    api.download_file(c, file_id, Path(tmp.name))
    transcripts = api.extract_note_text(tmp.name)
  use_ollama = args.ocr == "ollama"
  prefix = (args.output.rstrip("/") + "/") if args.output is not None else ""
  records = []
  for i, transcript in enumerate(transcripts):
    body = page_ocr.get(i + 1) or None
    annotation = body if use_ollama else None
    image_ref = None
    if args.output is not None:
      png_name = f"page_{i + 1}.png"
      if (Path(args.output) / png_name).exists():
        image_ref = f"{prefix}{png_name}"
    records.append({
      "page": i + 1,
      "transcript": transcript or None,
      "annotation": annotation,
      "handwritten_image": image_ref,
    })
  return records


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
  "annotation": _cmd_annotation,
  "an": _cmd_annotation,
  "source": _cmd_source,
  "notebook": _cmd_notebook,
  "nb": _cmd_notebook,
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
