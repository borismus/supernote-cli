import json
import os
import stat
import time
from pathlib import Path


def _config_dir() -> Path:
  base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
  return Path(base) / "supernote-cli"


def token_path() -> Path:
  return _config_dir() / "token.json"


def save(token: str, account: str) -> Path:
  p = token_path()
  p.parent.mkdir(parents=True, exist_ok=True)
  payload = {"token": token, "account": account, "created_at": int(time.time())}
  with open(p, "w") as f:
    json.dump(payload, f)
  os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
  return p


def load() -> dict | None:
  p = token_path()
  if not p.exists():
    return None
  try:
    with open(p) as f:
      return json.load(f)
  except (OSError, json.JSONDecodeError):
    return None


def clear() -> bool:
  p = token_path()
  if p.exists():
    p.unlink()
    return True
  return False
