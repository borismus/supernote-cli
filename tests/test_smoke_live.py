"""Live smoke tests against viewer.supernote.com.

Gated on SUPERNOTE_LIVE_TEST=1. Reads credentials from .env via Client.from_env.
Never runs in CI.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
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
  files = [n for n in contents if not n.is_folder]
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
  r = _run_cli("sync", "Note", "--out", str(tmp_path), "--dry-run", "--days-ago", "30")
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

  fake = Digest(id="0", content="x", source_path="Document/f.pdf", raw={})
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


def test_27_cli_digest_render(client: Client, tmp_path: Path):
  d = _first_digest_with_annotation(client)
  r = _run_cli("digest", d.id, "-o", str(tmp_path))
  assert r.returncode == 0, f"stderr: {r.stderr}"
  assert d.id in r.stdout
  # Expect a {id}.png or {id}_p1.png in the out dir
  pngs = list(tmp_path.glob(f"{d.id}*.png"))
  assert pngs, f"no PNG; stderr: {r.stderr}"
  assert "rendered:" in r.stderr


def test_28_cli_digest_no_annotation(client: Client, tmp_path: Path):
  d = _first_digest_with_annotation(client)
  r = _run_cli("digest", d.id, "-o", str(tmp_path), "--no-annotation")
  assert r.returncode == 0, f"stderr: {r.stderr}"
  assert d.id in r.stdout
  assert list(tmp_path.iterdir()) == [], "--no-annotation must not render"


def test_29_cli_digest_on_highlight_without_annotation(client: Client, tmp_path: Path):
  d = _first_digest_without_annotation(client)
  r = _run_cli("digest", d.id, "-o", str(tmp_path))
  assert r.returncode == 0, f"stderr: {r.stderr}"
  assert list(tmp_path.iterdir()) == []
  assert "no annotation" in r.stderr


def test_30_cli_digest_already_exists(client: Client, tmp_path: Path):
  d = _first_digest_with_annotation(client)
  r1 = _run_cli("digest", d.id, "-o", str(tmp_path))
  assert r1.returncode == 0
  assert "rendered:" in r1.stderr
  r2 = _run_cli("digest", d.id, "-o", str(tmp_path))
  assert r2.returncode == 0
  assert "already exists:" in r2.stderr
  assert "rendered:" not in r2.stderr
