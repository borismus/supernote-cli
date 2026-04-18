import re

from supernote_cli.auth import build_channel_header, hash_password
from supernote_cli.models import Note


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
