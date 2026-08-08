"""Compatibility shims for calling nemlig_cli from a stdio MCP server.

The CLI was written for a terminal: several of its API functions render
progress spinners with bare ``print()`` calls, and ``login()`` does so from a
background thread. On a stdio MCP server, stdout *is* the JSON-RPC transport,
so any stray byte written there corrupts the protocol stream.

``quiet()`` redirects stdout to stderr for the duration of a CLI call.
``print()`` resolves ``sys.stdout`` at call time and ``redirect_stdout``
rebinds that module attribute process-wide, so spinner threads are covered
too -- and ``Spinner.stop()`` joins its thread before returning, so nothing
escapes the context.

This shim exists so the rest of the package can call the CLI normally. If the
upstream CLI moves its progress output to stderr, delete this module and the
``quiet()`` wrappers in ``client.py``.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Route anything the CLI prints to stderr, keeping stdout protocol-clean."""
    with contextlib.redirect_stdout(sys.stderr):
        yield
