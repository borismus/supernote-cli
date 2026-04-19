import re
from datetime import datetime, timedelta

from PIL import Image

from supernote_cli import api, ocr
from supernote_cli.auth import build_channel_header, hash_password
from supernote_cli.models import Digest, Note


def test_hash_password_vector():
  # sha256(md5("hello")+"rc") computed externally
  # md5("hello") = 5d41402abc4b2a76b9719d911017c592
  # sha256("5d41402abc4b2a76b9719d911017c592rc") = ...
  import hashlib

  expected = hashlib.sha256(b"5d41402abc4b2a76b9719d911017c592rc").hexdigest()
  assert hash_password("hello", "rc") == expected


def test_channel_header_shape():
  token = "abc.def.ghi"
  eq = "MACOS_uuid-with-dashes"
  ch = build_channel_header(token, eq)
  # expected: {token}_{eq_stripped_of_underscores}_{ms_ts}
  assert ch.startswith(f"{token}_MACOSuuid-with-dashes_")
  # ends with a 13-digit ms timestamp
  assert re.match(rf"^{re.escape(token)}_MACOSuuid-with-dashes_\d{{13}}$", ch)


def test_note_from_api():
  n = Note.from_api(
    {
      "id": 12345,
      "directoryId": 0,
      "fileName": "test.note",
      "size": 100,
      "md5": "abc",
      "isFolder": "N",
      "createTime": 1700000000000,
      "updateTime": 1700000001000,
    }
  )
  assert n.id == "12345"
  assert n.directory_id == "0"
  assert n.file_name == "test.note"
  assert n.size == 100
  assert n.is_folder is False
  assert n.update_time.timestamp() == 1700000001.0


def test_note_from_api_folder():
  n = Note.from_api(
    {
      "id": 1,
      "directoryId": 0,
      "fileName": "Note",
      "size": 0,
      "md5": "",
      "isFolder": "Y",
      "createTime": 1700000000000,
      "updateTime": 1700000000000,
    }
  )
  assert n.is_folder is True


def test_digest_from_api_with_annotation():
  d = Digest.from_api(
    {
      "id": 555,
      "content": "a highlight",
      "sourcePath": "/Document/MyBook.pdf",
      "commentHandwriteName": "abc.mark",
      "lastModifiedTime": 1700000002000,
    }
  )
  assert d.id == "555"
  assert d.content == "a highlight"
  assert d.source_path == "/Document/MyBook.pdf"
  assert d.has_annotation is True
  assert d.last_modified_time == datetime.fromtimestamp(1700000002.0)


def test_digest_from_api_no_annotation_no_time():
  d = Digest.from_api(
    {
      "id": 777,
      "content": "bare highlight",
      "sourcePath": "/Document/X.pdf",
    }
  )
  assert d.has_annotation is False
  assert d.last_modified_time is None


def test_digest_from_api_last_modified_override():
  # Caller-supplied timestamp wins over raw payload
  override = datetime(2024, 1, 1, 12, 0, 0)
  d = Digest.from_api(
    {
      "id": 1,
      "content": "",
      "sourcePath": "/Document/X.pdf",
      "lastModifiedTime": 1700000000000,
    },
    last_modified_time=override,
  )
  assert d.last_modified_time == override


class _StubClient:
  """Captures _post calls and returns canned responses keyed by path."""

  def __init__(self, responses: dict[str, dict]):
    self._responses = responses
    self.calls: list[tuple[str, dict]] = []

  def _post(self, path: str, payload: dict, *, include_channel: bool = False) -> dict:
    self.calls.append((path, payload))
    return self._responses[path]


def _hash_payload(hashes):
  return {
    "summaryInfoVOList": [
      {
        "id": h["id"],
        "md5Hash": h.get("md5", ""),
        "lastModifiedTime": int(h["ts"].timestamp() * 1000),
      }
      for h in hashes
    ]
  }


def _summary_payload(digests):
  return {
    "summaryDOList": [
      {
        "id": d["id"],
        "content": d.get("content", ""),
        "sourcePath": d.get("sourcePath"),
        "commentHandwriteName": d.get("commentHandwriteName"),
      }
      for d in digests
    ]
  }


def test_list_digested_sources_groups_sorts_drops_orphans():
  now = datetime.now()
  earlier = now - timedelta(days=2)
  much_earlier = now - timedelta(days=60)
  c = _StubClient(
    {
      "file/query/summary/hash": _hash_payload(
        [
          {"id": "d1", "ts": now},
          {"id": "d2", "ts": earlier},
          {"id": "d3", "ts": now - timedelta(days=1)},
          {"id": "d4", "ts": much_earlier},  # will pass no date filter
          {"id": "d5", "ts": now},  # orphan (no sourcePath)
        ]
      ),
      "file/query/summary/id": _summary_payload(
        [
          {"id": "d1", "content": "c1", "sourcePath": "/Document/A.pdf"},
          {
            "id": "d2",
            "content": "c2",
            "sourcePath": "/Document/B.pdf",
            "commentHandwriteName": "ann.mark",
          },
          {"id": "d3", "content": "c3", "sourcePath": "/Document/A.pdf"},
          {"id": "d4", "content": "c4", "sourcePath": "/Document/A.pdf"},
          {"id": "d5", "content": "c5", "sourcePath": None},
        ]
      ),
    }
  )

  sources = api.list_digested_sources(c)
  by_path = {s.source_path: s for s in sources}
  # Orphan d5 dropped; A has 3 digests, B has 1
  assert set(by_path) == {"/Document/A.pdf", "/Document/B.pdf"}
  a = by_path["/Document/A.pdf"]
  assert a.source_stem == "A"
  assert len(a.digests) == 3
  # Sorted by last_modified_time desc within a source
  assert [d.id for d in a.digests] == ["d1", "d3", "d4"]
  # Sources sorted by latest_modified desc: A (d1=now) before B (d2=earlier)
  assert [s.source_path for s in sources] == [
    "/Document/A.pdf",
    "/Document/B.pdf",
  ]
  # has_annotation propagated
  assert by_path["/Document/B.pdf"].digests[0].has_annotation is True
  assert a.digests[0].has_annotation is False
  # last_modified_time stitched from hash payload
  assert a.digests[0].last_modified_time is not None


def test_list_digested_sources_days_ago_filter():
  now = datetime.now()
  c = _StubClient(
    {
      "file/query/summary/hash": _hash_payload(
        [
          {"id": "recent", "ts": now},
          {"id": "old", "ts": now - timedelta(days=30)},
        ]
      ),
      "file/query/summary/id": _summary_payload(
        [
          {"id": "recent", "content": "r", "sourcePath": "/Document/R.pdf"},
        ]
      ),
    }
  )
  sources = api.list_digested_sources(c, days_ago=7)
  assert len(sources) == 1
  assert sources[0].source_path == "/Document/R.pdf"
  # Only the recent id should have been fetched
  summary_call = next(p for path, p in c.calls if path == "file/query/summary/id")
  assert summary_call["ids"] == ["recent"]


def test_list_digested_sources_source_path_filter():
  now = datetime.now()
  c = _StubClient(
    {
      "file/query/summary/hash": _hash_payload(
        [
          {"id": "a1", "ts": now},
          {"id": "b1", "ts": now},
        ]
      ),
      "file/query/summary/id": _summary_payload(
        [
          {"id": "a1", "sourcePath": "/Document/A.pdf"},
          {"id": "b1", "sourcePath": "/Document/B.pdf"},
        ]
      ),
    }
  )
  sources = api.list_digested_sources(c, source_path="/Document/A.pdf")
  assert len(sources) == 1
  assert sources[0].source_path == "/Document/A.pdf"
  assert [d.id for d in sources[0].digests] == ["a1"]


def test_resize_for_ocr_no_op_when_small():
  img = Image.new("RGB", (400, 300), "white")
  out = ocr.resize_for_ocr(img, max_size=1024)
  assert out.size == (400, 300)


def test_resize_for_ocr_landscape_scales_to_max_width():
  img = Image.new("RGB", (2048, 1024), "white")
  out = ocr.resize_for_ocr(img, max_size=1024)
  assert out.size == (1024, 512)


def test_resize_for_ocr_portrait_scales_to_max_height():
  img = Image.new("RGB", (1024, 2048), "white")
  out = ocr.resize_for_ocr(img, max_size=1024)
  assert out.size == (512, 1024)


def test_image_to_base64_jpeg_returns_nonempty_str():
  img = Image.new("RGB", (32, 32), "white")
  s = ocr.image_to_base64_jpeg(img)
  assert isinstance(s, str) and len(s) > 0
  # Valid base64 decodes to JPEG SOI marker
  import base64 as _b64

  assert _b64.b64decode(s)[:3] == b"\xff\xd8\xff"
