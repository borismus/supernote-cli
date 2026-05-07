"""Local Ollama vision OCR for Supernote handwriting.

Calls a locally-running Ollama daemon (default http://localhost:11434)
with a handwriting-tuned prompt. `ocr_image` raises `OcrError` on failure
so callers can decide how to surface or downgrade.

Runtime dep: Ollama with a vision model pulled (default qwen3-vl:8b).
"""

from __future__ import annotations

import base64
import io
import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

import requests
from PIL import Image

OCR_PROMPT = """You are an OCR engine, not a writing assistant.

Task:
- Read the handwritten note in the image.
- Output the exact transcription of the text as plain markdown.

Critical constraints:
- Do NOT explain what you are doing.
- Do NOT think step-by-step.
- Do NOT describe, analyze, or comment on the note.
- Do NOT use phrases like "let's", "wait", "first line", "next line", "line X", "Got it", or "step by step".
- Do NOT mention spellings or say how words are written.
- Do NOT repeat any single word more than twice in a row.
- If you notice yourself repeating a word or phrase, immediately stop and output your best single transcription of the whole note.
- Your entire response must be ONLY the final transcription text, nothing else."""

DEFAULT_MODEL = "qwen3-vl:8b"
DEFAULT_MAX_SIZE = 1024
DEFAULT_TIMEOUT = 300


def default_host() -> str:
  return os.environ.get("OLLAMA_HOST", "http://localhost:11434")


class OcrError(Exception):
  """Raised when Ollama is unreachable or returns an error."""


def check_available(*, host: str | None = None, timeout: int = 5) -> None:
  """Ping Ollama at `host/api/tags`. Raises `OcrError` on any failure."""
  h = host or default_host()
  try:
    r = requests.get(f"{h}/api/tags", timeout=timeout)
    r.raise_for_status()
  except requests.RequestException as e:
    raise OcrError(f"Ollama not reachable at {h}. Start Ollama or pass --no-ocr.") from e


def resize_for_ocr(image: Image.Image, max_size: int = DEFAULT_MAX_SIZE) -> Image.Image:
  width, height = image.size
  if width <= max_size and height <= max_size:
    return image
  if width > height:
    new_width = max_size
    new_height = int(height * (max_size / width))
  else:
    new_height = max_size
    new_width = int(width * (max_size / height))
  return image.resize((new_width, new_height), Image.Resampling.LANCZOS)


def image_to_base64_jpeg(image: Image.Image, quality: int = 85) -> str:
  buffer = io.BytesIO()
  image.save(buffer, format="JPEG", quality=quality)
  return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _build_prompt(extra_prompt: str | None) -> str:
  """Combine the default OCR prompt with optional caller-supplied instructions.

  Caller text is appended after a clear section header so the model can
  treat it as additional rules layered on top of the OCR-engine guardrails.
  """
  if not extra_prompt:
    return OCR_PROMPT
  extra = extra_prompt.strip()
  if not extra:
    return OCR_PROMPT
  return f"{OCR_PROMPT}\n\nAdditional instructions:\n{extra}"


def ocr_base64(
  image_base64: str,
  *,
  model: str = DEFAULT_MODEL,
  host: str | None = None,
  timeout: int = DEFAULT_TIMEOUT,
  extra_prompt: str | None = None,
  on_token: Callable[[str], None] | None = None,
) -> str:
  """POST an already-base64-JPEG image to Ollama's chat endpoint.

  Returns the transcription string. Raises `OcrError` on transport error,
  HTTP error, or unexpected response shape. `extra_prompt`, if provided,
  is appended to the default OCR prompt under an "Additional instructions:"
  section — useful for project-specific transcription rules.

  When `on_token` is provided, the request is streamed and the callback
  is invoked once per Ollama chunk with the text delta. The full string
  is still returned. When `on_token` is None, behaves as a single
  blocking POST (no streaming overhead).
  """
  streaming = on_token is not None
  payload = {
    "model": model,
    "messages": [
      {
        "role": "user",
        "content": _build_prompt(extra_prompt),
        "images": [image_base64],
      }
    ],
    "stream": streaming,
  }
  try:
    response = requests.post(
      f"{host or default_host()}/api/chat",
      json=payload,
      timeout=timeout,
      stream=streaming,
    )
  except requests.RequestException as e:
    raise OcrError(f"Ollama request failed: {type(e).__name__}: {e}") from e

  if response.status_code != 200:
    detail = response.text
    try:
      detail = response.json().get("error", detail)
    except ValueError:
      pass
    raise OcrError(f"Ollama returned HTTP {response.status_code}: {detail}")

  if not streaming:
    try:
      data = response.json()
    except ValueError as e:
      raise OcrError(f"Ollama returned non-JSON: {response.text[:200]}") from e
    if "message" in data and "content" in data["message"]:
      return data["message"]["content"].strip()
    if "response" in data:
      return data["response"].strip()
    raise OcrError(f"Unexpected Ollama response shape: {data}")

  # Streaming: each line is a JSON object with a partial message.content.
  # Final chunk has done=true and may include a final aggregated message.
  #
  # Liveness: the model can take many seconds before the first token (image
  # encode + GPU model load on a cold call). To keep the CLI feeling alive,
  # a background thread emits a dot per second until either the first
  # content token arrives or the stream finishes. Dots go through `on_token`
  # but are NOT appended to `parts` — the returned string stays clean.
  parts: list[str] = []
  first_seen = False
  stop = threading.Event()

  def _tick():
    while not stop.wait(1.0):
      on_token(".")

  spinner = threading.Thread(target=_tick, daemon=True)
  spinner.start()
  try:
    for raw in response.iter_lines():
      if not raw:
        continue
      try:
        chunk = json.loads(raw)
      except ValueError:
        continue
      msg = chunk.get("message") or {}
      thinking = msg.get("thinking") or ""
      delta = msg.get("content") or chunk.get("response") or ""

      if thinking:
        # Surface fragmentary reasoning when the model emits it (Ollama
        # exposes this for thinking-capable models). Prefix once so it's
        # visually distinct from the final OCR output.
        if not first_seen:
          first_seen = True
          stop.set()
          on_token("\n")
        on_token(thinking)
      if delta:
        if not first_seen:
          first_seen = True
          stop.set()
          on_token("\n")
        on_token(delta)
        parts.append(delta)
      if chunk.get("done"):
        break
  finally:
    stop.set()
    spinner.join(timeout=0.1)
  return "".join(parts).strip()


def ocr_image(
  image: Image.Image | str | Path,
  *,
  model: str = DEFAULT_MODEL,
  max_size: int = DEFAULT_MAX_SIZE,
  host: str | None = None,
  timeout: int = DEFAULT_TIMEOUT,
  extra_prompt: str | None = None,
  on_token: Callable[[str], None] | None = None,
) -> str:
  """Run OCR on an image path or PIL Image via local Ollama vision.

  Raises `OcrError` on any failure. `extra_prompt` is appended to the
  default OCR prompt — used for project-specific transcription rules
  (e.g. "preserve lines starting with → or ☐ verbatim"). When `on_token`
  is provided, the call streams from Ollama and invokes the callback
  per chunk so callers can render progress as it arrives.
  """
  if isinstance(image, (str, Path)):
    img = Image.open(image)
  else:
    img = image
  img = resize_for_ocr(img, max_size=max_size)
  payload = image_to_base64_jpeg(img)
  return ocr_base64(
    payload,
    model=model,
    host=host,
    timeout=timeout,
    extra_prompt=extra_prompt,
    on_token=on_token,
  )
