"""Local entrypoint: `python -m app`.

Binds to loopback by default. TradingView cannot reach a loopback address, so
publishing the webhook requires the operator's own authenticated HTTPS ingress
on port 443 in front of this process (TradingView accepts ports 80 and 443 only
and cancels deliveries that take longer than three seconds).
"""

from __future__ import annotations

import uvicorn

from app.api.main import create_app
from app.config import get_settings


# Access logging is OFF, and this is a security control rather than a
# preference: the webhook shared secret is the final path segment, because
# TradingView cannot send custom headers. An access log would therefore write
# the token to disk on every delivery. Do not turn this on. If request-level
# visibility is needed, log the correlation id, which the engine already emits.
ACCESS_LOG_ENABLED = False


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=ACCESS_LOG_ENABLED,
        # Keeps the server from advertising its version in responses.
        server_header=False,
    )


if __name__ == "__main__":
    main()
