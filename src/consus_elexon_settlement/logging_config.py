"""Logging setup for the gunicorn-served apps.

ITS OWN MODULE, AND STDLIB ONLY. The obvious home was `app.py`, beside
`require_env` and `build_cipher` -- but `app.py` imports `db`, which imports
`psycopg`. `ems/echo.py` exists precisely because it has no database in its
import graph, and reaching into `app.py` for one function would put one there.
Nothing below imports anything but the standard library, so both endpoints can
use it without acquiring each other's dependencies.
"""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """Make this package's loggers visible under gunicorn.

    WITHOUT THIS, EVERY log.info IN A SERVED APP IS DISCARDED. `cli.py` calls
    `logging.basicConfig` after parsing arguments, so the Jobs are fine; a
    gunicorn-served app never runs that path, the root logger stays at
    WARNING, and INFO records are dropped with no error anywhere.

    Measured, not theorised: the first message across the EMS bridge returned
    HTTP 200 having parsed a 48-period profile correctly, and logged nothing
    but gunicorn's access line. The parse had to be confirmed by counting the
    bytes in the response body, which is not a diagnostic.

    `force=True` because gunicorn installs handlers on the root logger before
    importing the app, and basicConfig is a no-op when handlers already exist
    -- which is exactly the case that made this silent.
    """
    logging.basicConfig(
        level=os.environ.get("CONSUS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
