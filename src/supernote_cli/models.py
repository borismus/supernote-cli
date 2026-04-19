from dataclasses import dataclass
from datetime import datetime


@dataclass
class Note:
  id: str
  directory_id: str
  file_name: str
  size: int
  md5: str
  is_folder: bool
  create_time: datetime
  update_time: datetime

  @classmethod
  def from_api(cls, data: dict) -> "Note":
    return cls(
      id=str(data["id"]),
      directory_id=str(data["directoryId"]),
      file_name=data["fileName"],
      size=int(data.get("size") or 0),
      md5=data.get("md5") or "",
      is_folder=data.get("isFolder") == "Y",
      create_time=datetime.fromtimestamp(data["createTime"] / 1000),
      update_time=datetime.fromtimestamp(data["updateTime"] / 1000),
    )


@dataclass
class DigestHash:
  id: str
  md5_hash: str
  last_modified: datetime

  @classmethod
  def from_api(cls, data: dict) -> "DigestHash":
    return cls(
      id=str(data["id"]),
      md5_hash=data.get("md5Hash") or "",
      last_modified=datetime.fromtimestamp(data["lastModifiedTime"] / 1000),
    )


@dataclass
class Digest:
  id: str
  content: str
  source_path: str | None
  has_annotation: bool
  last_modified_time: datetime | None
  raw: dict

  @classmethod
  def from_api(cls, data: dict, *, last_modified_time: datetime | None = None) -> "Digest":
    mt = last_modified_time
    if mt is None:
      ts = data.get("lastModifiedTime")
      if ts:
        mt = datetime.fromtimestamp(ts / 1000)
    return cls(
      id=str(data["id"]),
      content=data.get("content") or "",
      source_path=data.get("sourcePath"),
      has_annotation=bool(data.get("commentHandwriteName")),
      last_modified_time=mt,
      raw=data,
    )
