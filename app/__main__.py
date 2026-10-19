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


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
