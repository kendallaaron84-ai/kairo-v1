class RiskGovernorError(Exception):
    """Base class for deterministic governor failures."""


class InvalidStateTransition(RiskGovernorError):
    """Raised when a manual state transition violates the state machine."""


class RiskSessionNotInitialized(RiskGovernorError):
    """Raised when an operation requires a current persisted risk session."""
