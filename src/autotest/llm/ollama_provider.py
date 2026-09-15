"""Ollama REST API provider."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from autotest.errors import LLMConnectionError, LLMError, LLMResponseError
from autotest.llm.base import LLMProvider


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    """Connection and generation settings for local Ollama."""

    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5-coder:14b"
    timeout: float = 120.0
    temperature: float = 0.0


class OllamaProvider(LLMProvider):
    """Generate text through Ollama's non-streaming HTTP endpoint."""

    def __init__(self, config: OllamaConfig | None = None) -> None:
        self.config = config or OllamaConfig()

    def generate(self, prompt: str) -> str:
        payload = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.config.temperature},
        }
        request = urllib.request.Request(
            url=f"{self.config.base_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                response_body = response.read()
        except urllib.error.HTTPError as exc:
            raise LLMError(f"Ollama returned HTTP {exc.code}: {exc.reason}") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise LLMConnectionError(
                f"Ollama request timed out after {self.config.timeout:g} seconds."
            ) from exc
        except (urllib.error.URLError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise LLMConnectionError(
                    f"Ollama request timed out after {self.config.timeout:g} seconds."
                ) from exc
            raise LLMConnectionError(
                f"Could not connect to Ollama at {self.config.base_url}: {reason}"
            ) from exc
        except OSError as exc:
            raise LLMConnectionError(f"Ollama request failed: {exc}") from exc

        data = self._parse_response(response_body)
        generated = data.get("response")
        if not isinstance(generated, str):
            raise LLMResponseError("Ollama response is missing the string field 'response'.")
        if not generated.strip():
            raise LLMResponseError("Ollama returned an empty model response.")
        return generated

    @staticmethod
    def _parse_response(response_body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMResponseError("Ollama returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise LLMResponseError("Ollama returned a JSON value that is not an object.")
        return data
