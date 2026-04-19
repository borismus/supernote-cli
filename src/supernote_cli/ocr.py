"""Local Ollama vision OCR for Supernote handwriting.

Calls a locally-running Ollama daemon (default http://localhost:11434)
with a handwriting-tuned prompt. Returns the transcription string, or
None on any transport/parse error (so callers can fall back cleanly).

Runtime dep: Ollama with a vision model pulled (default qwen3-vl:8b).
"""

from __future__ import annotations

import base64
import io
import traceback
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
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_TIMEOUT = 300


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


def ocr_base64(
  image_base64: str,
  *,
  model: str = DEFAULT_MODEL,
  host: str = DEFAULT_HOST,
  timeout: int = DEFAULT_TIMEOUT,
) -> str | None:
  """POST an already-base64-JPEG image to Ollama's chat endpoint."""
  try:
    response = requests.post(
      f"{host}/api/chat",
      json={
        "model": model,
        "messages": [
          {
            "role": "user",
            "content": OCR_PROMPT,
            "images": [image_base64],
          }
        ],
        "stream": False,
      },
      timeout=timeout,
    )

    if response.status_code != 200:
      error_detail = response.text
      try:
        error_detail = response.json().get("error", error_detail)
      except Exception:
        pass
      print(f"Error from ollama API (status {response.status_code}): {error_detail}")
      return None

    data = response.json()
    if "message" in data and "content" in data["message"]:
      return data["message"]["content"].strip()
    if "response" in data:
      return data["response"].strip()
    print(f"Warning: Unexpected response format from ollama API: {data}")
    return None
  except requests.exceptions.Timeout as e:
    print(f"Error: OCR request timed out after {timeout} seconds: {e}")
    return None
  except requests.exceptions.ConnectionError as e:
    print(f"Error: Could not connect to ollama API at {host}: {e}")
    return None
  except requests.exceptions.RequestException as e:
    print(f"Error calling ollama API: {type(e).__name__}: {e}")
    return None
  except Exception as e:
    print(f"Unexpected error during OCR: {type(e).__name__}: {e}")
    traceback.print_exc()
    return None


def ocr_image(
  image: Image.Image | str | Path,
  *,
  model: str = DEFAULT_MODEL,
  max_size: int = DEFAULT_MAX_SIZE,
  host: str = DEFAULT_HOST,
  timeout: int = DEFAULT_TIMEOUT,
) -> str | None:
  """Run OCR on an image path or PIL Image via local Ollama vision.

  Returns the transcription string, or None if OCR failed for any reason
  (network, model error, unexpected response). Never raises.
  """
  if isinstance(image, (str, Path)):
    img = Image.open(image)
  else:
    img = image
  img = resize_for_ocr(img, max_size=max_size)
  payload = image_to_base64_jpeg(img)
  return ocr_base64(payload, model=model, host=host, timeout=timeout)
