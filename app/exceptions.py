class AgentError(Exception):
    """Base class for all errors the agent raises deliberately (never a raw traceback to the user)."""


class DatabaseUnavailableError(AgentError):
    pass


class RequestNotFoundError(AgentError):
    pass


class NotRaiseCapacityRequestError(AgentError):
    pass


class StudentNotFoundError(AgentError):
    pass


class CourseNotFoundError(AgentError):
    pass


class SectionNotFoundError(AgentError):
    pass


class SectionCourseMismatchError(AgentError):
    pass


class KnowledgeBaseError(AgentError):
    pass


class PrerequisiteSourceUnavailableError(AgentError):
    """Raised internally when the UoS page cannot be fetched or parsed; callers convert this into a
    verified=False evidence result rather than letting it abort the whole evaluation."""


class GeminiUnavailableError(AgentError):
    """Raised internally when Gemini cannot be reached; callers fall back to a deterministic summary."""
