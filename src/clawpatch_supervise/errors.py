class ClawpatchSuperviseError(Exception):
    """Base error."""


class ConfigurationError(ClawpatchSuperviseError):
    """Configuration is invalid or incomplete."""


class SafetyError(ClawpatchSuperviseError):
    """A safety invariant was violated."""


class RepositoryBusyError(SafetyError):
    """Another verified process currently owns the repository workflow."""


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


class GateFailure(SafetyError):
    """A deterministic verification gate failed."""
