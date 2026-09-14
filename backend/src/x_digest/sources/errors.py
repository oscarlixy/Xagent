class XSourceError(Exception):
    """A source failure whose fields are safe to report to an operator."""

    def __init__(
        self, status_code: int, code: str, message: str, *, request_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id


class AuthenticationError(XSourceError):
    def __init__(self, *, request_id: str | None = None) -> None:
        super().__init__(
            401,
            "x_authorization_failed",
            "X access is not authorized",
            request_id=request_id,
        )


class RateLimitError(XSourceError):
    def __init__(self, *, request_id: str | None = None) -> None:
        super().__init__(429, "x_rate_limited", "X access is rate limited", request_id=request_id)


class UpstreamError(XSourceError):
    def __init__(self, *, request_id: str | None = None) -> None:
        super().__init__(
            503,
            "x_upstream_unavailable",
            "X access is temporarily unavailable",
            request_id=request_id,
        )


class InvalidResponseError(XSourceError):
    def __init__(self, *, request_id: str | None = None) -> None:
        super().__init__(
            502,
            "x_response_invalid",
            "X returned an invalid response",
            request_id=request_id,
        )


class InvalidRequestError(XSourceError):
    def __init__(self) -> None:
        super().__init__(400, "x_request_invalid", "X request is invalid")
