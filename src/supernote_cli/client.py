import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from . import auth, tokenstore
from .auth import API_BASE, DEFAULT_EQUIPMENT_NO


class ApiError(Exception):
  def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
    super().__init__(message)
    self.code = code
    self.status = status


class AuthRequired(ApiError):
  """Raised when no valid credentials are available to authenticate."""


class Client:
  """HTTP client for viewer.supernote.com.

  The client holds a session token and can transparently re-authenticate on
  HTTP 401 / E0401 responses when `.env` credentials are available.
  """

  def __init__(
    self,
    token: str | None = None,
    *,
    account: str | None = None,
    password: str | None = None,
    equipment_no: str | None = None,
    no_cache: bool = False,
    verbose: bool = False,
  ):
    self.token = token
    self.account = account
    self._password = password
    self.equipment_no = equipment_no or os.environ.get(
      "SUPERNOTE_EQUIPMENT_NO", DEFAULT_EQUIPMENT_NO
    )
    self.no_cache = no_cache
    self.verbose = verbose

  @classmethod
  def from_env(cls, *, no_cache: bool = False, verbose: bool = False) -> "Client":
    """Build a client from `.env` (walk-up from CWD) plus an optional token cache."""
    load_dotenv()
    account = os.environ.get("SUPERNOTE_USER")
    password = os.environ.get("SUPERNOTE_PASSWORD")
    equipment_no = os.environ.get("SUPERNOTE_EQUIPMENT_NO")

    token = None
    if not no_cache:
      cached = tokenstore.load()
      if cached and cached.get("account") == account:
        token = cached.get("token")

    return cls(
      token=token,
      account=account,
      password=password,
      equipment_no=equipment_no,
      no_cache=no_cache,
      verbose=verbose,
    )

  def login(self) -> str:
    if not self.account or not self._password:
      raise AuthRequired("SUPERNOTE_USER and SUPERNOTE_PASSWORD must be set in .env or environment")
    self.token = auth.login(self.account, self._password, equipment_no=self.equipment_no)
    if not self.no_cache:
      tokenstore.save(self.token, self.account)
    return self.token

  def logout(self) -> bool:
    self.token = None
    return tokenstore.clear()

  def _headers(self, *, include_channel: bool = False) -> dict:
    h = auth.base_headers(self.equipment_no, token=self.token)
    if include_channel and self.token:
      h["channel"] = auth.build_channel_header(self.token, self.equipment_no)
    return h

  def _post(
    self,
    path: str,
    payload: dict,
    *,
    include_channel: bool = False,
    extra_headers: dict | None = None,
    _retry: bool = True,
  ) -> dict:
    if not self.token:
      self.login()
    headers = self._headers(include_channel=include_channel)
    if extra_headers:
      headers.update(extra_headers)
    r = requests.post(
      API_BASE + path,
      json=payload,
      headers=headers,
      timeout=30,
    )
    if self.verbose:
      print(f"POST {path} -> HTTP {r.status_code}")

    if r.status_code == 401 and _retry and self._password:
      self.token = None
      self.login()
      return self._post(
        path, payload, include_channel=include_channel,
        extra_headers=extra_headers, _retry=False,
      )

    try:
      data = r.json()
    except ValueError as e:
      raise ApiError(
        f"non-JSON response from {path}: HTTP {r.status_code} {r.text[:200]}",
        status=r.status_code,
      ) from e

    # Some endpoints return 200 with success=false + errorCode=E0401 for expired tokens
    if data.get("errorCode") == "E0401" and _retry and self._password:
      self.token = None
      self.login()
      return self._post(
        path, payload, include_channel=include_channel,
        extra_headers=extra_headers, _retry=False,
      )

    if data.get("success") is False:
      raise ApiError(
        data.get("errorMsg") or f"{path} failed",
        code=data.get("errorCode"),
        status=r.status_code,
      )
    return data

  def get_binary(self, url: str) -> bytes:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content

  def download_to(self, url: str, path: Path, chunk_size: int = 1 << 16) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with requests.get(url, timeout=120, stream=True) as r:
      r.raise_for_status()
      with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size):
          if chunk:
            f.write(chunk)
            total += len(chunk)
    return total

  def put_binary(self, url: str, path: Path, headers: dict) -> int:
    """Stream `path` to `url` with a PUT, attaching `headers`.

    Used to upload to the signed S3 URL returned by `file/upload/apply`.
    Returns the number of bytes sent.
    """
    size = path.stat().st_size
    with open(path, "rb") as f:
      r = requests.put(url, data=f, headers=headers, timeout=600)
    if self.verbose:
      print(f"PUT {url} -> HTTP {r.status_code} ({size} bytes)")
    r.raise_for_status()
    return size
