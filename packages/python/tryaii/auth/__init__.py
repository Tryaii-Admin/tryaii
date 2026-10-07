"""CLI sign-in for tryaii.com (docs/auth/CONTRACT-auth-v1.md, section 6).

Device authorization grant (RFC 8628) over stdlib urllib: ``login`` prints a
URL + code and polls, ``whoami`` refreshes and asks the server who we are,
``logout`` revokes and deletes the local credentials file.
"""

from tryaii.auth.transport import (
    NETWORK,
    RATE_LIMITED,
    AuthError,
    effective_api_url,
    session_api_url,
)

__all__ = ["AuthError", "NETWORK", "RATE_LIMITED", "effective_api_url",
           "session_api_url"]
