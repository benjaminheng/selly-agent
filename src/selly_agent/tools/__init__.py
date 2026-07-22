"""The typed MCP tool surface.

Importing this package registers every tool (each module calls register() at import). The
server imports it once at startup; tests import it to exercise the registry. Keep this import
list complete — an unimported tool module is a tool that silently doesn't exist.
"""

from __future__ import annotations

from . import messaging, publish, reads, writes  # noqa: F401  imported for registration
from .registry import (  # noqa: F401  re-exported as the package's public surface
    TIER_ATTENDED,
    TIER_PASS_PUBLISH,
    Session,
    ToolContext,
    ToolError,
    ToolSpec,
    UnknownTool,
    all_specs,
    dispatch,
    tools_for_tier,
)
