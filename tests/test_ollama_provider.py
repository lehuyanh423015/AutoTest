import io
import json
import socket
import urllib.error
from unittest.mock import patch

import pytest

from autotest.errors import LLMConnectionError, LLMError, LLMResponseError
from autotest.llm.ollama_provider import OllamaConfig, OllamaProvider


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_valid_response_and_request_configuration() -> None:
    config = OllamaConfig(
        base_url="http://ollama.test/",
        model="test-model",
        timeout=7.0,
        temperature=0.25,
    )
    provider = OllamaProvider(config)
    response = FakeResponse(json.dumps({"response": "test code"}).encode())

    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        assert provider.generate("a prompt") == "test code"

    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == "http://ollama.test/api/generate"
    assert request.method == "POST"
    assert payload == {
        "model": "test-model",
        "prompt": "a prompt",
        "stream": False,
        "options": {"temperature": 0.25},
    }
    assert urlopen.call_args.kwargs["timeout"] == 7.0


def test_connection_failure_is_wrapped() -> None:
    error = urllib.error.URLError(ConnectionRefusedError("refused"))

    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(LLMConnectionError, match="Could not connect"):
            OllamaProvider().generate("prompt")


@pytest.mark.parametrize(
    "error",
    [socket.timeout("timed out"), urllib.error.URLError(socket.timeout("timed out"))],
)
def test_timeout_is_reported_clearly(error: BaseException) -> None:
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(LLMConnectionError, match="timed out after 120 seconds"):
            OllamaProvider().generate("prompt")


@pytest.mark.parametrize("payload", [b"not json", b"[]"])
def test_malformed_response_is_rejected(payload: bytes) -> None:
    with patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
        with pytest.raises(LLMResponseError):
            OllamaProvider().generate("prompt")


@pytest.mark.parametrize("body", [{}, {"response": ""}, {"response": "   "}])
def test_missing_or_empty_response_is_rejected(body: dict[str, str]) -> None:
    payload = json.dumps(body).encode()

    with patch("urllib.request.urlopen", return_value=FakeResponse(payload)):
        with pytest.raises(LLMResponseError):
            OllamaProvider().generate("prompt")


def test_http_error_is_reported() -> None:
    error = urllib.error.HTTPError(
        "http://localhost:11434/api/generate", 500, "server error", {}, io.BytesIO()
    )

    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(LLMError, match="HTTP 500"):
            OllamaProvider().generate("prompt")
