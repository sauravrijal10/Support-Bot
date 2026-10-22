"""Domain errors raised by services. main.py maps each to an HTTP status, so
business logic never has to import FastAPI."""


class DomainError(Exception):
    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class NotFoundError(DomainError):
    status_code = 404


class ConflictError(DomainError):
    status_code = 409


class InvalidRequestError(DomainError):
    status_code = 422


class UnauthorizedError(DomainError):
    """Not signed in, or the credentials are no longer valid."""

    status_code = 401
    headers = {"WWW-Authenticate": "Bearer"}


class ServiceUnavailableError(DomainError):
    """An upstream dependency (e.g. the LLM provider) is failing or busy."""

    status_code = 503
    headers = {"Retry-After": "5"}
