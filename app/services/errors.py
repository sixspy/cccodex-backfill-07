class ServiceError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        reason_code: str | None = None,
        details: dict | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.reason_code = reason_code
        self.details = details or {}
        super().__init__(message)
