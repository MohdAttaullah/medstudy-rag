from typing import Any


class DomainError(Exception):
    def __init__(
        self, code: str, message: str, status: int = 400, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(code)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}
