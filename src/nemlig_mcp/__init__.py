"""MCP server for nemlig.com grocery shopping, built on nemlig-cli."""

from .client import CredentialsError, NemligClient

__version__ = "0.1.0"

__all__ = ["CredentialsError", "NemligClient", "__version__"]
