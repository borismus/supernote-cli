"""Vision OCR for Supernote handwriting, via an OpenAI-compatible server.

Point `SUPERNOTE_OCR_BASE_URL` at any server exposing `/v1/chat/completions`
with image content parts — an MLX server on another Mac, llama.cpp, vLLM — and
set `SUPERNOTE_OCR_API_KEY` if it needs one. Both can also be passed directly
to `ocr_image`.

These prompts carry the full text of private handwritten notes, so point this
at a model on hardware you control.

`ocr_image` raises `OcrError` on failure so callers can decide how to surface
or downgrade.
"""

from __future__ import annotations

import base64
import io
import os
import time
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

DEFAULT_MODEL = "Qwen3.8-27B-MLX-4bit"
DEFAULT_MAX_SIZE = 1024
DEFAULT_TIMEOUT = 300
# Generous: a dense page plus whatever a reasoning model spends thinking.
# Hitting it raises rather than returning a truncated transcription.
DEFAULT_MAX_TOKENS = 4096

# The server may be on another machine that drops off the network briefly.
# Callers collapse a failed page to "no text", which is indistinguishable from
# a genuinely blank page — so retry here rather than let a blip look like an
# empty note.
RETRY_DELAYS = (2, 5, 15)


def default_base_url() -> str | None:
  return os.environ.get("SUPERNOTE_OCR_BASE_URL") or None


def default_api_key() -> str | None:
  return os.environ.get("SUPERNOTE_OCR_API_KEY") or None


class OcrError(Exception):
  """Raised when the OCR server is unreachable or returns an error."""


def _resolve(base_url: str | None, api_key: str | None) -> tuple[str, str | None]:
  resolved = base_url or default_base_url()
  if not resolved:
    raise OcrError(
      "No OCR server configured. Set SUPERNOTE_OCR_BASE_URL (and "
      "SUPERNOTE_OCR_API_KEY if required), or pass base_url=."
    )
  return resolved, (api_key or default_api_key())


def check_available(
  *,
  base_url: str | None = None,
  api_key: str | None = None,
  timeout: int = 15,
) -> None:
  """GET `{base_url}/models`. Raises `OcrError` if it stays unreachable.

  Retries on the same schedule as a real request: a preflight check that gave
  up faster than the work it guards would abort runs that would have succeeded.
  """
  resolved, key = _resolve(base_url, api_key)
  headers = {"Authorization": f"Bearer {key}"} if key else {}
  url = resolved.rstrip("/") + "/models"
  last_error: Exception | None = None
  for delay in (*RETRY_DELAYS, None):
    try:
      requests.get(url, headers=headers, timeout=timeout).raise_for_status()
      return
    except requests.RequestException as e:
      last_error = e
    if delay is None:
      break
    time.sleep(delay)
  raise OcrError(f"OCR server not reachable at {resolved}.") from last_error


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
  base_url: str | None = None,
  api_key: str | None = None,
  timeout: int = DEFAULT_TIMEOUT,
  extra_prompt: str | None = None,
  on_token: Callable[[str], None] | None = None,
) -> str:
  """POST an already-base64-JPEG image as an image content part.

  Returns the transcription. Raises `OcrError` on transport error, HTTP error,
  or unexpected response shape. `extra_prompt`, if provided, is appended to the
  default OCR prompt under an "Additional instructions:" section — useful for
  project-specific transcription rules.

  The request is not streamed; `on_token`, if given, receives the finished text
  in a single call, so a caller's progress UI updates once per page.
  """
  resolved, key = _resolve(base_url, api_key)

  payload = {
    "model": model,
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": _build_prompt(extra_prompt)},
          {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"},
          },
        ],
      }
    ],
    "max_tokens": DEFAULT_MAX_TOKENS,
  }
  headers = {"Content-Type": "application/json"}
  if key:
    headers["Authorization"] = f"Bearer {key}"

  url = resolved.rstrip("/") + "/chat/completions"
  response = None
  last_error = ""
  for delay in (*RETRY_DELAYS, None):
    try:
      response = requests.post(url, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as e:
      last_error = f"request to {resolved} failed: {type(e).__name__}: {e}"
    else:
      if response.status_code == 200:
        break
      # 4xx is the request's fault (bad model, bad auth) and won't fix itself.
      if response.status_code < 500:
        raise OcrError(
          f"OCR server returned HTTP {response.status_code}: {response.text[:300]}"
        )
      last_error = f"server returned HTTP {response.status_code}: {response.text[:200]}"

    if delay is None:
      raise OcrError(f"OCR failed after {len(RETRY_DELAYS) + 1} attempts — {last_error}")
    time.sleep(delay)

  try:
    choice = response.json()["choices"][0]
  except (ValueError, KeyError, IndexError) as e:
    raise OcrError(f"Unexpected OCR response shape: {response.text[:300]}") from e

  # A reasoning model that runs out of budget mid-thought spills its scratchpad
  # into `content`. Returning that would file the model's deliberation as if it
  # were the transcription, so treat it as a failure instead.
  if choice.get("finish_reason") == "length":
    raise OcrError(
      f"Model {model!r} hit the {DEFAULT_MAX_TOKENS}-token limit before finishing; "
      f"the page may be unusually dense."
    )

  text = (choice.get("message", {}).get("content") or "").strip()
  if on_token and text:
    on_token(text)
  return text


def ocr_image(
  image: Image.Image | str | Path,
  *,
  model: str = DEFAULT_MODEL,
  max_size: int = DEFAULT_MAX_SIZE,
  base_url: str | None = None,
  api_key: str | None = None,
  timeout: int = DEFAULT_TIMEOUT,
  extra_prompt: str | None = None,
  on_token: Callable[[str], None] | None = None,
) -> str:
  """Run OCR on an image path or PIL Image.

  Raises `OcrError` on any failure. `extra_prompt` is appended to the default
  OCR prompt — used for project-specific transcription rules (e.g. "preserve
  lines starting with → or ☐ verbatim").
  """
  img = Image.open(image) if isinstance(image, (str, Path)) else image
  img = resize_for_ocr(img, max_size=max_size)
  return ocr_base64(
    image_to_base64_jpeg(img),
    model=model,
    base_url=base_url,
    api_key=api_key,
    timeout=timeout,
    extra_prompt=extra_prompt,
    on_token=on_token,
  )
