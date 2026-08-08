"""Authenticated access to the nemlig.com API via the nemlig_cli library.

Nemlig's bearer token expires after 300 seconds, so a long-lived MCP process
cannot log in once at startup. This module keeps a cached ``AuthTokens``,
refreshes it proactively before it goes stale, and retries once on a 401 in
case the server expired it early.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import nemlig_cli
import requests
from nemlig_cli import AuthTokens

from ._compat import quiet

T = TypeVar("T")

# Nemlig issues bearer tokens with expires_in=300. Refresh early so a token
# cannot lapse mid-request.
TOKEN_TTL_SECONDS = 240


class CredentialsError(RuntimeError):
    """Raised when no nemlig.com credentials could be found."""


def _load_credentials() -> tuple[str, str]:
    """Resolve credentials from the environment, then the CLI's config file.

    Environment wins so an MCP client can override per-server config without
    touching ~/.config/nemlig/login.json.
    """
    username = os.environ.get("NEMLIG_USER")
    password = os.environ.get("NEMLIG_PASS")

    if not (username and password):
        try:
            config = nemlig_cli.load_config_credentials()
        except (ValueError, OSError) as exc:
            raise CredentialsError(
                f"Could not read {nemlig_cli.CONFIG_FILE}: {exc}"
            ) from exc
        username = username or config.get("username")
        password = password or config.get("password")

    if not (username and password):
        raise CredentialsError(
            "No nemlig.com credentials found. Set NEMLIG_USER and NEMLIG_PASS, "
            f"or create {nemlig_cli.CONFIG_FILE} with "
            '{"username": "...", "password": "..."}.'
        )

    return username, password


class NemligClient:
    """Thread-safe, self-refreshing wrapper around the nemlig_cli API layer."""

    def __init__(self, username: str | None = None, password: str | None = None):
        if username and password:
            self._username, self._password = username, password
        else:
            self._username, self._password = _load_credentials()

        self._tokens: AuthTokens | None = None
        self._issued_at: float = 0.0
        self._lock = threading.Lock()

    def _fresh_tokens(self, force: bool = False) -> AuthTokens:
        with self._lock:
            expired = time.monotonic() - self._issued_at >= TOKEN_TTL_SECONDS
            if force or self._tokens is None or expired:
                with quiet():
                    self._tokens = nemlig_cli.login(self._username, self._password)
                self._issued_at = time.monotonic()
            return self._tokens

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Invoke a nemlig_cli API function, passing fresh auth as first argument.

        Retries once with a forced re-login if the API rejects the token, which
        can happen if it expired earlier than advertised.
        """
        try:
            with quiet():
                return func(self._fresh_tokens(), *args, **kwargs)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in (401, 403):
                raise
            with quiet():
                return func(self._fresh_tokens(force=True), *args, **kwargs)
