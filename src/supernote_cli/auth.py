import time
from hashlib import md5, sha256

import requests

API_BASE = "https://viewer.supernote.com/api/"
DEFAULT_EQUIPMENT_NO = "MACOS_f5ca5c0c-be0b-4a2c-89b4-48410fc9c11d"


class AuthError(Exception):
  pass


def hash_password(password: str, random_code: str) -> str:
  return sha256((md5(password.encode()).hexdigest() + random_code).encode()).hexdigest()


def build_channel_header(token: str, equipment_no: str) -> str:
  eq = equipment_no.replace("_", "")
  return f"{token}_{eq}_{int(time.time() * 1000)}"


def base_headers(equipment_no: str, token: str | None = None) -> dict:
  h = {
    "content-type": "application/json",
    "version": "202407",
    "equipmentNo": equipment_no,
  }
  if token:
    h["x-access-token"] = token
  return h


def _post(path: str, payload: dict, headers: dict, timeout: int = 30) -> dict:
  r = requests.post(API_BASE + path, json=payload, headers=headers, timeout=timeout)
  try:
    data = r.json()
  except ValueError as e:
    raise AuthError(f"non-JSON response from {path}: HTTP {r.status_code} {r.text[:200]}") from e
  return data


def login(email: str, password: str, equipment_no: str = DEFAULT_EQUIPMENT_NO) -> str:
  """Authenticate against viewer.supernote.com and return an access token."""
  headers = base_headers(equipment_no)
  data = _post(
    "official/user/query/random/code",
    {"account": email, "countryCode": None},
    headers,
  )
  if not data.get("success"):
    raise AuthError(f"random code request failed: {data.get('errorMsg') or data}")
  rc = data["randomCode"]
  timestamp = data["timestamp"]

  data = _post(
    "official/user/account/login/new",
    {
      "account": email,
      "equipment": 2,
      "loginMethod": 2,
      "password": hash_password(password, rc),
      "countryCode": None,
      "equipmentNo": None,
      "timestamp": timestamp,
      "browser": None,
      "language": "EN",
      "devices": "MACOS",
    },
    headers,
  )
  if not data.get("success"):
    raise AuthError(f"login failed: {data.get('errorMsg') or data}")
  return data["token"]
