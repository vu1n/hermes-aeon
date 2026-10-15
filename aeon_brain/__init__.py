"""Aeon Brain — MCP-first memory service.

The brain is a standalone product: any MCP host (Claude, an IDE, a custom bot)
or any brain.general.v1 client can use it. Hermes is one client among many,
never the host. This package depends on brain_service and the stdlib only;
importing it must never pull in the Hermes host runtime or the plugin.

Entry points (run from the operator repository root):

* ``python3 -m aeon_brain.mcp --db PATH``   MCP stdio server owning the
  canonical DB (trusted local mode; principal from UID bindings or an
  explicit inline principal).
* ``python3 -m aeon_brain.mcp --socket PATH``  MCP stdio server proxying the
  gateway daemon; no DB access, identity from this process's OS credentials.
* ``python3 -m aeon_brain.server ...``  Gateway daemon (brain.general.v1 over
  a Unix socket; see docs/brain-mcp.md).
"""
