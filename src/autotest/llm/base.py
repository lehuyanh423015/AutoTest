"""Provider-neutral LLM interface."""

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Minimal interface implemented by text-generation providers."""

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Generate text for a prompt or raise an LLMError."""
        raise NotImplementedError
