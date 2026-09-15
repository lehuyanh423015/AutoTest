"""Application-level exceptions raised by AutoTest."""


class AutoTestError(Exception):
    """Base class for expected AutoTest failures."""


class AnalyzerError(AutoTestError):
    """Raised when a target source file cannot be analyzed."""


class FunctionNotFoundError(AnalyzerError):
    """Raised when the requested top-level function does not exist."""


class LLMError(AutoTestError):
    """Base class for LLM provider failures."""


class LLMConnectionError(LLMError):
    """Raised when the configured LLM service cannot be reached."""


class LLMResponseError(LLMError):
    """Raised when an LLM service returns an unusable response."""


class GenerationError(AutoTestError):
    """Raised when generated test code cannot be produced or saved."""


class ArtifactError(AutoTestError):
    """Raised when immutable run artifacts cannot be persisted."""


class TestExecutionError(AutoTestError):
    """Raised when test execution cannot be configured safely."""


class CoverageError(AutoTestError):
    """Raised when coverage execution or report parsing cannot complete safely."""


class MutationError(AutoTestError):
    """Raised when mutation evaluation cannot complete safely."""


class MutationEnvironmentUnavailableError(MutationError):
    """Raised when the isolated mutation toolchain is unavailable or incompatible."""
