"""Live smoke tests against viewer.supernote.com.

Gated on SUPERNOTE_LIVE_TEST=1. Reads credentials from .env via Client.from_env.
Never runs in CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from supernote_cli import api, tokenstore
from supernote_cli.auth import AuthError
from supernote_cli.auth import login as raw_login
from supernote_cli.client import ApiError, Client

LIVE = os.environ.get("SUPERNOTE_LIVE_TEST") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="set SUPERNOTE_LIVE_TEST=1 to run")


@pytest.fixture(scope="module")
def client() -> Client:
  c = Client.from_env()
  if not c.token:
    c.login()
  return c


# ---------- Auth ----------


def test_01_login_succeeds(client: Client):
  assert client.token
  assert client.account and "@" in client.account


def test_02_login_bad_password_fails():
  account = os.environ["SUPERNOTE_USER"]
  with pytest.raises(AuthError):
    raw_login(account, "definitely-not-the-real-password-xyz123")


def test_03_token_cache_roundtrip(client: Client, tmp_path, monkeypatch):
  # Redirect XDG so we don't clobber the real cache
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  p = tokenstore.save(client.token, client.account)
  assert p.exists()
  mode = p.stat().st_mode & 0o777
  assert mode == stat.S_IRUSR | stat.S_IWUSR, f"expected 0600, got {oct(mode)}"
  loaded = tokenstore.load()
  assert loaded["token"] == client.token
  assert loaded["account"] == client.account


def test_04_401_triggers_reauth(tmp_path, monkeypatch):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  c = Client.from_env()
  c.token = "not-a-real-token"  # corrupt
  # Any authenticated call should auto re-auth via .env creds
  files = api.list_files(c, 0)
  assert isinstance(files, list)
  # Token should now be different from our corruption
  assert c.token != "not-a-real-token"


def test_05_no_cache_flag(tmp_path, monkeypatch):
  monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
  c = Client.from_env(no_cache=True)
  c.login()
  assert c.token
  assert not tokenstore.token_path().exists(), "no_cache=True must not write cache"


# ---------- Listing & paths ----------


def test_06_ls_root(client: Client):
  root = api.list_files(client, 0)
  assert len(root) > 0
  names = {n.file_name for n in root}
  assert "Note" in names, f"expected a 'Note' folder at root, got {names}"


def test_07_ls_note_folder(client: Client):
  _, contents = api.resolve_path(client, "Note")
  assert len(contents) > 0
  # Not asserting there must be files (could be all folders), but fields must decode
  for n in contents[:5]:
    assert n.id
    assert n.file_name


def test_08_resolve_path(client: Client):
  dir_id, _ = api.resolve_path(client, "Note")
  assert str(dir_id) != "0"
  with pytest.raises(ApiError):
    api.resolve_path(client, "Note/definitely-nonexistent-folder-xyz")


def test_09_ls_root_via_empty_path(client: Client):
  dir_id, contents = api.resolve_path(client, "")
  assert dir_id == 0
  assert any(n.file_name == "Note" and n.is_folder for n in contents)


# ---------- Download ----------


def _pick_small_note(client: Client):
  _, contents = api.resolve_path(client, "Note")
  files = [n for n in contents if not n.is_folder]
  if not files:
    pytest.skip("no files under Note to download")
  # Smallest wins — keeps the test fast
  return min(files, key=lambda n: n.size)


def test_10_download_single(client: Client, tmp_path: Path):
  target = _pick_small_note(client)
  dest = tmp_path / target.file_name
  n = api.download_file(client, target.id, dest)
  assert n == target.size
  actual_md5 = hashlib.md5(dest.read_bytes()).hexdigest()
  assert actual_md5 == target.md5, f"md5 mismatch: {actual_md5} vs {target.md5}"


def test_11_download_preserves_mtime(client: Client, tmp_path: Path):
  entries = api.sync_folder(client, "Note", tmp_path, dry_run=False)
  downloaded = [e for e in entries if e.action == "download"]
  if not downloaded:
    pytest.skip("no downloads to check mtime on")
  e = downloaded[0]
  local_ts = e.local_path.stat().st_mtime
  remote_ts = e.note.update_time.timestamp()
  assert abs(local_ts - remote_ts) < 1.0, f"mtime drift: {local_ts} vs {remote_ts}"


# ---------- Sync ----------


def test_12_sync_dry_run(client: Client, tmp_path: Path):
  entries = api.sync_folder(client, "Note", tmp_path, dry_run=True)
  plan = [e for e in entries if e.action == "download"]
  assert len(plan) > 0
  # Nothing actually written
  assert not any(tmp_path.iterdir())


def test_13_sync_skips_uptodate(client: Client, tmp_path: Path):
  first = api.sync_folder(client, "Note", tmp_path, days_ago=7)
  downloads_first = [e for e in first if e.action == "download"]
  if not downloads_first:
    pytest.skip("no recent files to exercise re-sync skip path")
  second = api.sync_folder(client, "Note", tmp_path, days_ago=7)
  downloads_second = [e for e in second if e.action == "download"]
  assert downloads_second == [], "second sync should download 0 new files"
  assert any(e.action == "skip:uptodate" for e in second)


def test_14_sync_days_ago_filters(client: Client, tmp_path: Path):
  # Past 30 days and past 1 day — 1-day window should be <= 30-day window
  thirty = api.sync_folder(client, "Note", tmp_path, days_ago=30, dry_run=True)
  one = api.sync_folder(client, "Note", tmp_path, days_ago=1, dry_run=True)
  n30 = sum(1 for e in thirty if e.action == "download")
  n1 = sum(1 for e in one if e.action == "download")
  assert n1 <= n30


# ---------- Digests ----------


def test_15_digest_hashes(client: Client):
  hashes = api.fetch_digest_hashes(client, size=10)
  assert len(hashes) > 0
  for h in hashes[:5]:
    assert h.id
    assert h.md5_hash
    assert h.last_modified.timestamp() > 0


def test_16_digests_by_id(client: Client):
  hashes = api.fetch_digest_hashes(client, size=5)
  ids = [h.id for h in hashes[:3]]
  digests = api.fetch_digests_by_ids(client, ids)
  assert len(digests) > 0
  # At least one should have non-empty content
  assert any(d.content for d in digests)


def test_17_digest_source_path_shape(client: Client):
  hashes = api.fetch_digest_hashes(client, size=3)
  digests = api.fetch_digests_by_ids(client, [h.id for h in hashes])
  for d in digests:
    # sourcePath is either None or a string — document whichever we see
    assert d.source_path is None or isinstance(d.source_path, str)


# ---------- CLI smoke (subprocess) ----------


def _run_cli(*args, cwd: Path | None = None) -> subprocess.CompletedProcess:
  # Use the project's own uv env
  project_dir = Path(__file__).resolve().parent.parent
  return subprocess.run(
    ["uv", "run", "--project", str(project_dir), "supernote", *args],
    cwd=cwd or project_dir,
    capture_output=True,
    text=True,
    timeout=120,
  )


def test_18_cli_whoami():
  # After module fixture has logged in, whoami should work
  r = _run_cli("whoami")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  assert "account:" in r.stdout


def test_19_cli_ls():
  r = _run_cli("ls")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  assert "Note" in r.stdout


def test_20_cli_digest_ls():
  r = _run_cli("digest", "ls", "--limit", "2")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  assert r.stdout.count("\n") >= 1


def test_21_cli_sync_dry_run(tmp_path):
  r = _run_cli("sync", "Note", "-o", str(tmp_path), "--dry-run", "--days-ago", "30")
  assert r.returncode == 0, f"stderr: {r.stderr}"


def test_22_cli_digest_ls_json():
  r = _run_cli("digest", "ls", "--limit", "2", "--json")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  data = json.loads(r.stdout)
  assert isinstance(data, list)
  assert len(data) <= 2


# ---------- Digest → PNG render ----------


def _first_digest_with_annotation(client: Client):
  hashes = api.fetch_digest_hashes(client, size=50)
  digests = api.fetch_digests_by_ids(client, [h.id for h in hashes[:30]])
  d = next((d for d in digests if d.raw.get("commentHandwriteName")), None)
  if d is None:
    pytest.skip("no digest with commentHandwriteName available")
  return d


def _first_digest_without_annotation(client: Client):
  hashes = api.fetch_digest_hashes(client, size=50)
  digests = api.fetch_digests_by_ids(client, [h.id for h in hashes[:30]])
  d = next((d for d in digests if not d.raw.get("commentHandwriteName")), None)
  if d is None:
    pytest.skip("no highlight-only digest available")
  return d


def test_23_render_handwriting_single_page(client: Client, tmp_path: Path):
  d = _first_digest_with_annotation(client)
  paths = api.render_handwriting(client, d, tmp_path)
  assert len(paths) >= 1
  p = paths[0]
  assert p.exists()
  # single-page filename is bare {id}.png; multi-page would be _p{N}
  assert p.name == f"{d.id}.png" or p.name.startswith(f"{d.id}_p")
  # PNG magic bytes
  assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_24_render_handwriting_none_when_no_comment(client: Client, tmp_path: Path):
  from supernote_cli.models import Digest

  fake = Digest(
    id="0",
    content="x",
    source_path="Document/f.pdf",
    has_annotation=False,
    last_modified_time=None,
    raw={},
  )
  assert api.render_handwriting(client, fake, tmp_path) == []
  assert list(tmp_path.iterdir()) == []


def test_25_render_handwriting_skip_existing(client: Client, tmp_path: Path):
  d = _first_digest_with_annotation(client)
  first = api.render_handwriting(client, d, tmp_path)
  assert first, "first render should write at least one file"
  mtimes = {p: p.stat().st_mtime_ns for p in first}
  second = api.render_handwriting(client, d, tmp_path)
  assert second == [], "second call should skip and return []"
  # Existing files not modified
  for p, mt in mtimes.items():
    assert p.stat().st_mtime_ns == mt, f"{p} was re-written"


def test_26_render_handwriting_force_rewrites(client: Client, tmp_path: Path):
  d = _first_digest_with_annotation(client)
  first = api.render_handwriting(client, d, tmp_path)
  assert first
  before = {p: p.stat().st_mtime_ns for p in first}
  import time as _time

  _time.sleep(0.01)
  rewritten = api.render_handwriting(client, d, tmp_path, force=True)
  assert rewritten == first, "force should rewrite the same page(s)"
  for p, mt in before.items():
    assert p.stat().st_mtime_ns >= mt, f"{p} should have been re-written"


def test_27_cli_digest_json_shape(client: Client, tmp_path: Path):
  """`digest <id>` with annotation prints JSON object using Supernote terms."""
  d = _first_digest_with_annotation(client)
  r = _run_cli("digest", d.id, "-o", str(tmp_path), "--no-ocr")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  data = json.loads(r.stdout)
  assert data["id"] == d.id
  assert "digest" in data  # highlight content
  assert data["annotation"] is None, "--no-ocr must null out annotation"
  assert data["handwritten_image"], "expected handwritten_image path in JSON"
  assert "source_path" in data
  assert "last_modified" in data
  # PNG really on disk under {-o}/attachments/
  img = data["handwritten_image"]
  paths = [img] if isinstance(img, str) else img
  for rel in paths:
    assert (tmp_path / rel).exists(), f"missing: {tmp_path / rel}"


def test_28_cli_digest_no_handwriting(client: Client, tmp_path: Path):
  """`digest <id>` on a highlight-only digest returns null image + annotation."""
  d = _first_digest_without_annotation(client)
  r = _run_cli("digest", d.id, "-o", str(tmp_path), "--no-ocr")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  data = json.loads(r.stdout)
  assert data["id"] == d.id
  assert data["handwritten_image"] is None
  assert data["annotation"] is None
  # No attachments directory contents created for an empty render
  attachments = tmp_path / "attachments"
  if attachments.exists():
    assert list(attachments.iterdir()) == []


def test_29_cli_digest_multi_ids_array(client: Client, tmp_path: Path):
  """Multiple comma-separated ids produce a JSON array."""
  d1 = _first_digest_with_annotation(client)
  d2 = _first_digest_without_annotation(client)
  r = _run_cli("digest", f"{d1.id},{d2.id}", "-o", str(tmp_path), "--no-ocr")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  data = json.loads(r.stdout)
  assert isinstance(data, list)
  assert {x["id"] for x in data} == {d1.id, d2.id}


def test_30_cli_digest_skip_existing(client: Client, tmp_path: Path):
  """Second invocation should not rewrite the PNG; handwritten_image still points to it."""
  d = _first_digest_with_annotation(client)
  r1 = _run_cli("digest", d.id, "-o", str(tmp_path), "--no-ocr")
  assert r1.returncode == 0
  data1 = json.loads(r1.stdout)
  img_rel = data1["handwritten_image"]
  paths1 = [img_rel] if isinstance(img_rel, str) else img_rel
  mtimes = {p: (tmp_path / p).stat().st_mtime_ns for p in paths1}

  r2 = _run_cli("digest", d.id, "-o", str(tmp_path), "--no-ocr")
  assert r2.returncode == 0
  data2 = json.loads(r2.stdout)
  assert data2["handwritten_image"] == img_rel
  for p, mt in mtimes.items():
    assert (tmp_path / p).stat().st_mtime_ns == mt, f"{p} was re-written without --force"


def test_31_render_handwriting_file_path(client: Client, tmp_path: Path):
  """API-level: `out` ending in .png should still work for programmatic users."""
  d = _first_digest_with_annotation(client)
  target = tmp_path / "my_annotation.png"
  paths = api.render_handwriting(client, d, target)
  assert paths == [target] or (paths and paths[0] == target)
  assert target.exists()
  assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# ---------- Sources ----------


def test_34_list_digested_sources(client: Client):
  sources = api.list_digested_sources(client, days_ago=365)
  if not sources:
    pytest.skip("no digested sources in the last year")
  for s in sources[:3]:
    assert s.source_path
    assert s.source_stem
    assert s.digests, "each source should carry at least one digest"
    assert s.latest_modified.timestamp() > 0
    for d in s.digests:
      assert d.last_modified_time is not None, "timestamp stitched from hash"
      assert d.source_path == s.source_path
  # Sorted most-recent first
  mods = [s.latest_modified for s in sources]
  assert mods == sorted(mods, reverse=True)


def test_35_list_digested_sources_source_path_filter(client: Client):
  sources = api.list_digested_sources(client, days_ago=365)
  if not sources:
    pytest.skip("no digested sources")
  target_path = sources[0].source_path
  filtered = api.list_digested_sources(client, source_path=target_path)
  assert len(filtered) == 1
  assert filtered[0].source_path == target_path


def test_36_cli_source_ls():
  r = _run_cli("source", "ls", "--limit", "3")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  # Either an empty-state message or at least one row
  assert r.stdout.strip(), f"expected output; stderr: {r.stderr}"


def test_37_cli_source_ls_json():
  r = _run_cli("source", "ls", "--limit", "3", "--json")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  data = json.loads(r.stdout)
  assert isinstance(data, list)
  assert len(data) <= 3
  for entry in data:
    assert set(entry) == {
      "source_path",
      "source_stem",
      "digest_count",
      "latest_modified",
    }


# ---------- Local .note OCR ----------


def _download_smallest_note(client, tmp_path):
  entries = api.sync_folder(client, "Note", tmp_path, days_ago=30)
  downloaded = [e for e in entries if e.action == "download"]
  if not downloaded:
    downloaded = [e for e in entries if e.local_path.exists()]
  if not downloaded:
    pytest.skip("no .note file available under Note")
  return min(downloaded, key=lambda e: e.note.size).local_path


def test_38_render_note(client, tmp_path):
  note_path = _download_smallest_note(client, tmp_path / "notes")
  out = tmp_path / "rendered"
  pngs = api.render_note(note_path, out)
  assert len(pngs) >= 1
  for i, p in enumerate(pngs, start=1):
    assert p.name == f"page_{i}.png"
    assert p.exists()
    assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_39_render_note_skip_existing(client, tmp_path):
  note_path = _download_smallest_note(client, tmp_path / "notes")
  out = tmp_path / "rendered"
  first = api.render_note(note_path, out)
  mtimes = {p: p.stat().st_mtime_ns for p in first}
  second = api.render_note(note_path, out)
  assert [p.name for p in second] == [p.name for p in first]
  for p, mt in mtimes.items():
    assert p.stat().st_mtime_ns == mt


def test_40_extract_note_text_shape(client, tmp_path):
  note_path = _download_smallest_note(client, tmp_path / "notes")
  transcripts = api.extract_note_text(note_path)
  assert isinstance(transcripts, list)
  for t in transcripts:
    assert isinstance(t, str)


def _ollama_up() -> bool:
  import requests as _rq

  try:
    _rq.get("http://localhost:11434/api/tags", timeout=1)
    return True
  except Exception:
    return False


@pytest.mark.skipif(not _ollama_up(), reason="Ollama not reachable")
def test_41_ocr_note(client, tmp_path):
  note_path = _download_smallest_note(client, tmp_path / "notes")
  out = tmp_path / "ocr"
  pages = api.ocr_note(note_path, out)
  assert len(pages) >= 1
  for i, page in enumerate(pages, start=1):
    assert page.index == i
    assert page.png_path.exists()
    assert page.transcript is None or isinstance(page.transcript, str)
    assert page.ocr_text is None or isinstance(page.ocr_text, str)


def _smallest_cloud_note_id(client):
  pairs = api.list_notes(client, folder_path="Note", recursive=True)
  if not pairs:
    pytest.skip("no .note files under Note")
  _, note = min(pairs, key=lambda pn: pn[1].size)
  return note.id


def test_42_list_notes(client):
  pairs = api.list_notes(client, folder_path="Note", recursive=True)
  assert len(pairs) > 0
  for folder_path, note in pairs[:5]:
    assert isinstance(folder_path, str)
    assert note.file_name.endswith(".note")
    assert not note.is_folder


def test_43_ocr_note_from_cloud(client, tmp_path):
  file_id = _smallest_cloud_note_id(client)
  out = tmp_path / "cloud_ocr"
  pages = api.ocr_note_from_cloud(client, file_id, out)
  assert len(pages) >= 1
  for i, p in enumerate(pages, start=1):
    assert p.index == i
    assert p.png_path.exists()
    assert p.png_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_44_cli_note_ls_json(client):
  r = _run_cli("note", "ls", "--limit", "3", "--json")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  data = json.loads(r.stdout)
  assert isinstance(data, list)
  assert len(data) <= 3
  for entry in data:
    assert set(entry) == {"id", "folder_path", "file_name", "size", "update_time"}
    assert entry["file_name"].endswith(".note")


def test_45_cli_note_by_id_json(client, tmp_path):
  """`note <id>` emits JSON array with v0.2 schema; PNGs under {-o}/attachments/."""
  file_id = _smallest_cloud_note_id(client)
  out = tmp_path / "cli_cloud_ocr"
  r = _run_cli("note", file_id, "-o", str(out), "--no-ocr")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  data = json.loads(r.stdout)
  assert isinstance(data, list)
  assert len(data) >= 1
  for entry in data:
    assert set(entry) == {"page", "transcript", "annotation", "handwritten_image"}
    assert entry["annotation"] is None, "--no-ocr must null out annotation"
    assert entry["handwritten_image"].startswith("attachments/")
    assert (out / entry["handwritten_image"]).exists()


# ---------- Upload round-trip (self-cleaning) ----------

UPLOAD_TEST_DIR = "Document"  # v0.1 test-area; files are cleaned up in `finally`


def _upload_test_filename() -> str:
  import uuid
  return f"_supernote_cli_test_{uuid.uuid4().hex[:8]}.txt"


def _unique_bytes(prefix: bytes = b"cli-e2e") -> bytes:
  """Fresh per call — avoids Supernote's server-side md5 dedup."""
  import uuid
  return prefix + b"\n" + uuid.uuid4().bytes * 16


def test_46_upload_roundtrip(client: Client, tmp_path: Path):
  """Upload a tiny local file, verify via ls, download, md5 match, delete."""
  name = _upload_test_filename()
  src = tmp_path / name
  payload = _unique_bytes(b"roundtrip")
  src.write_bytes(payload)
  expected_md5 = hashlib.md5(payload).hexdigest()

  note = None
  try:
    note = api.upload_file(client, src, UPLOAD_TEST_DIR)
    assert note.file_name == name
    assert note.size == len(payload)

    # Appears in ls
    _, contents = api.resolve_path(client, UPLOAD_TEST_DIR)
    assert any(n.file_name == name and not n.is_folder for n in contents)

    # Download and compare
    dest = tmp_path / "roundtrip" / name
    api.download_file(client, note.id, dest)
    assert hashlib.md5(dest.read_bytes()).hexdigest() == expected_md5
  finally:
    if note is not None:
      api.delete_file(client, note)


def test_47_upload_missing_remote_dir_errors(client: Client, tmp_path: Path):
  """Uploading to a nonexistent folder fails cleanly (no auto-mkdir)."""
  src = tmp_path / "nope.txt"
  src.write_bytes(b"x")
  with pytest.raises(ApiError):
    api.upload_file(client, src, "Document/_definitely_does_not_exist_xyz")


def test_48_upload_skip_then_overwrite(client: Client, tmp_path: Path):
  """Second upload without --overwrite errors; with --overwrite it replaces."""
  name = _upload_test_filename()
  src = tmp_path / name
  first_bytes = _unique_bytes(b"first")
  src.write_bytes(first_bytes)
  uploaded_notes: list = []
  try:
    first = api.upload_file(client, src, UPLOAD_TEST_DIR)
    uploaded_notes.append(first)

    # Same name again -> error
    with pytest.raises(ApiError, match="already exists"):
      api.upload_file(client, src, UPLOAD_TEST_DIR)

    # With overwrite -> new note, old id no longer resolvable
    second_bytes = _unique_bytes(b"second")
    src.write_bytes(second_bytes)
    second = api.upload_file(client, src, UPLOAD_TEST_DIR, overwrite=True)
    assert second.id != first.id
    assert second.size == len(second_bytes)
    uploaded_notes = [second]  # first was deleted by overwrite path
  finally:
    for n in uploaded_notes:
      try:
        api.delete_file(client, n)
      except ApiError:
        pass


def test_49_cli_upload_roundtrip(client: Client, tmp_path: Path):
  """CLI upload verb: happy path + visible via ls, cleaned up afterward."""
  name = _upload_test_filename()
  src = tmp_path / name
  src.write_bytes(_unique_bytes(b"cli-smoke"))

  uploaded_note = None
  try:
    r = _run_cli("upload", str(src), UPLOAD_TEST_DIR + "/")
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert name in r.stdout
    # Resolve note for cleanup
    _, contents = api.resolve_path(client, UPLOAD_TEST_DIR)
    uploaded_note = next((n for n in contents if n.file_name == name and not n.is_folder), None)
    assert uploaded_note is not None, "uploaded file not visible via ls"
  finally:
    if uploaded_note is not None:
      api.delete_file(client, uploaded_note)


def test_50_cli_download_path_based(client: Client, tmp_path: Path):
  """`download <remote-path>` (without --by-id) resolves by path."""
  # Upload a seed so we know exactly what path to download
  name = _upload_test_filename()
  src = tmp_path / name
  payload = _unique_bytes(b"path-dl")
  src.write_bytes(payload)
  expected_md5 = hashlib.md5(payload).hexdigest()

  note = None
  try:
    note = api.upload_file(client, src, UPLOAD_TEST_DIR)

    dest = tmp_path / "dl" / name
    r = _run_cli("download", f"{UPLOAD_TEST_DIR}/{name}", "-o", str(dest))
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert dest.exists()
    assert hashlib.md5(dest.read_bytes()).hexdigest() == expected_md5
  finally:
    if note is not None:
      api.delete_file(client, note)


def test_51_cli_download_by_id(client: Client, tmp_path: Path):
  """`download --by-id <id>` keeps working as an escape hatch."""
  target = _pick_small_note(client)
  dest = tmp_path / "byid.note"
  r = _run_cli("download", "--by-id", target.id, "-o", str(dest))
  assert r.returncode == 0, f"stderr: {r.stderr}"
  assert dest.exists()
  assert dest.stat().st_size == target.size


# ---------- Fixture-based .note round-trip ----------

_FIXTURE_NOTE = Path(__file__).parent / "fixtures" / "sample.note"


@pytest.mark.skipif(not _FIXTURE_NOTE.exists(), reason=f"{_FIXTURE_NOTE} missing")
def test_52_fixture_note_roundtrip(client: Client, tmp_path: Path):
  """Upload tests/fixtures/sample.note, render pages, download back, md5 match, delete.

  Populate the fixture once (from any `.note` in your account):
    supernote note ls --json --limit 1 | jq -r '.[0].id' | \\
      xargs -I{} supernote download --by-id {} -o tests/fixtures/sample.note
  """
  name = _upload_test_filename().replace(".txt", ".note")
  remote_dir = UPLOAD_TEST_DIR  # .note files can live under /Document
  expected_md5 = hashlib.md5(_FIXTURE_NOTE.read_bytes()).hexdigest()

  note = None
  try:
    # Copy fixture to a named path so the remote filename matches
    staged = tmp_path / name
    staged.write_bytes(_FIXTURE_NOTE.read_bytes())

    try:
      note = api.upload_file(client, staged, remote_dir)
    except ApiError as e:
      if "identical md5" in str(e):
        pytest.skip(
          "server deduped by md5 — the fixture .note already exists in your "
          "cloud. Replace tests/fixtures/sample.note with a .note that isn't "
          "already uploaded."
        )
      raise

    # Download by path
    dest = tmp_path / "rt" / name
    api.download_file(client, note.id, dest)
    assert hashlib.md5(dest.read_bytes()).hexdigest() == expected_md5

    # Render pages (doesn't require Ollama)
    pngs = api.render_note(dest, tmp_path / "rendered")
    assert pngs, "expected at least one page rendered from fixture .note"
    for p in pngs:
      assert p.exists()
      assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
  finally:
    if note is not None:
      api.delete_file(client, note)
