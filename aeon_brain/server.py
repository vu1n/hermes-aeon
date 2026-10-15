"""Aeon Brain gateway daemon: brain.general.v1 over a local Unix socket.

Thin deployment entrypoint over brain_service.server — the trusted store
component stays in one place. The daemon opens the operator-provided canonical
database (never creates or migrates it), binds OS UIDs to principals through an
operator-owned bindings file, and serves bounded one-request connections.
Clients get socket access only: no canonical files, credentials or execution.

    python3 -m aeon_brain.server --db /var/lib/aeon-canonical/aeon.db \\
        --socket /run/aeon-general/general.sock --bindings /etc/aeon-brain/bindings.json

See docs/brain-mcp.md for the deployment and rollback contract.
"""
from brain_service.server import Server, bindings, main

if __name__ == "__main__":
    raise SystemExit(main())
