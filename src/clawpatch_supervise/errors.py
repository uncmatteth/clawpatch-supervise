class ClawpatchSuperviseError(Exception):
    """Base error."""


class ConfigurationError(ClawpatchSuperviseError):
    """Configuration is invalid or incomplete."""


class SafetyError(ClawpatchSuperviseError):
    """A safety invariant was violated."""


class StateTransitionError(ClawpatchSuperviseError):
    """An invalid state transition was attempted."""


class ContextBudgetError(ClawpatchSuperviseError):
    """Required context cannot fit within the configured budget."""


class AgentExecutionError(ClawpatchSuperviseError):
    """An external coding agent failed or returned invalid output."""


class ValidationError(ClawpatchSuperviseError):
    """An artifact or agent response failed validation."""


class BlockingDecisionError(ClawpatchSuperviseError):
    """A product decision must be made before implementation can proceed."""


class GateFailure(ClawpatchSuperviseError):
    """A deterministic verification gate failed."""
