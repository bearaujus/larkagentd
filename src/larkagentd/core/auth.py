"""Credential provider errors shared by the gateway and tools."""

import httpx


class AuthError(Exception):
    def __init__(self, code, *, status=None, operation=None, retry_after=None):
        super().__init__(code)
        self.status = status
        self.operation = operation
        self.retry_after = retry_after


def transient_auth_error(error):
    return isinstance(
        error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
    ) or (
        isinstance(error, AuthError)
        and isinstance(error.status, int)
        and (error.status in (408, 429) or 500 <= error.status <= 599)
    )
