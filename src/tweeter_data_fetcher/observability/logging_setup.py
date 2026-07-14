#!/usr/bin/env python3
"""Process-wide logging configuration for ``tweeter_data_fetcher``.

This module owns the **file** channel; :class:`PipelineConsole` owns the
**terminal** (Rich).  They are deliberately split:

* ``configure_logging`` attaches a single rotating ``.log`` file handler +
  a verbosity-gated ``stderr`` handler to the root logger
  ``tweeter_data_fetcher``.
* :class:`PipelineConsole` emits its semantic messages through child loggers
  ``tweeter_data_fetcher.console.<subsystem>`` that **propagate** to the root
  logger — so every console line also lands in the log file without a second
  file handler (no duplicated writes, no double-printed terminal lines).
* :class:`RunIdFilter` stamps every record with ``run_id`` so a single grep
  threads the file log together with the JSONL event stream
  (:class:`EventRecorder`).

Typical use (from a CLI ``main`` or :meth:`TimelineFetcher.__init__`)::

    configure_logging(subsystem="historical", logs_dir=logs_dir, verbosity=verb)
    ...
    attach_run_id(run_id)  # once the run id is known
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from tweeter_data_fetcher.observability.pipeline_console import Verbosity

# Single root logger for the whole package. Child loggers (console.<sub>,
# twitter.client, pipelines.historical.service, ...) propagate to it.
ROOT_LOGGER_NAME = "tweeter_data_fetcher"
CONSOLE_LOGGER_PREFIX = f"{ROOT_LOGGER_NAME}.console"

# Map semantic verbosity to the stderr tail level. The file handler is always
# DEBUG so the on-disk record is complete regardless of terminal noise.
_VERBOSITY_TO_LEVEL = {
    Verbosity.QUIET: logging.WARNING,
    Verbosity.NORMAL: logging.INFO,
    Verbosity.VERBOSE: logging.DEBUG,
}

_FILE_FMT = logging.Formatter(
    "%(asctime)s run=%(run_id)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_STDERR_FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Module-global handles so configure_logging is idempotent and attach_run_id
# can mutate the live filter without the caller threading the object around.
_RUN_FILTER: Optional["RunIdFilter"] = None
_CONFIGURED = False


class RunIdFilter(logging.Filter):
    """Stamp every log record with ``record.run_id`` (default ``"-"``)."""

    def __init__(self, run_id: str = "-") -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


class _NoConsoleOnStderr(logging.Filter):
    """Drop console-subsystem records from the stderr tail.

    :class:`PipelineConsole` already prints those lines to the Rich terminal,
    so echoing them to stderr would double-print. The root file handler is
    unaffected (records still propagate there), keeping the log file complete.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(CONSOLE_LOGGER_PREFIX)


def configure_logging(
    *,
    subsystem: str,
    logs_dir: Optional[Path],
    verbosity: Verbosity = Verbosity.NORMAL,
    run_id: Optional[str] = None,
) -> logging.Logger:
    """Configure the package root logger once (idempotent).

    First caller wins for ``subsystem``/``logs_dir``/``verbosity`` (the CLI
    ``main`` runs before the engine is built and sets the right values).
    Subsequent calls still update ``run_id``. Safe to call with ``logs_dir``
    ``None`` (stderr-only) — used by tests/CLIs that don't persist logs.
    """
    global _RUN_FILTER, _CONFIGURED
    root = logging.getLogger(ROOT_LOGGER_NAME)

    if not _CONFIGURED:
        # Clean slate: a previous basicConfig/test run may have added handlers.
        for handler in list(root.handlers):
            root.removeHandler(handler)

        _RUN_FILTER = RunIdFilter(run_id or "-")
        stderr_handler = logging.StreamHandler(stream=sys.stderr)
        stderr_handler.setLevel(_VERBOSITY_TO_LEVEL.get(verbosity, logging.INFO))
        stderr_handler.setFormatter(_STDERR_FMT)
        stderr_handler.addFilter(_NoConsoleOnStderr())
        stderr_handler.addFilter(_RUN_FILTER)
        root.addHandler(stderr_handler)

        if logs_dir is not None:
            logs_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                logs_dir / f"{subsystem}.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(_FILE_FMT)
            file_handler.addFilter(_RUN_FILTER)
            root.addHandler(file_handler)

        root.setLevel(logging.DEBUG)
        root.propagate = False  # we own stderr; don't bubble to logging.root
        _CONFIGURED = True
    elif run_id and _RUN_FILTER is not None:
        _RUN_FILTER.run_id = run_id

    return root


def attach_run_id(run_id: Optional[str]) -> None:
    """Stamp ``run_id`` onto every subsequent log record.

    Call once the run id is known (it is created *after* the engine is built
    in the historical pipeline). No-op before :func:`configure_logging`.
    """
    if _RUN_FILTER is not None and run_id:
        _RUN_FILTER.run_id = run_id


def reset_logging() -> None:
    """Tear down configured handlers (test-only isolation)."""
    global _RUN_FILTER, _CONFIGURED
    root = logging.getLogger(ROOT_LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
    _RUN_FILTER = None
    _CONFIGURED = False
