"""MCP stdio surface of the Aeon Brain.

One process, two modes, identical tool surface:

* ``--socket PATH`` — proxy mode. Every call is forwarded over
  brain.general.v1 to the gateway daemon. This process holds no database
  access; the daemon resolves the principal from this process's OS peer
  credentials (SO_PEERCRED), exactly like any other client.
* ``--db PATH`` — trusted local mode. This process owns the canonical
  database and speaks the same Gateway boundary in-process. The principal is
  resolved from this process's UID through an operator-owned bindings file,
  or given explicitly for single-user setups.

The server never creates, migrates or bootstraps a database; a missing
general schema is a refusal to start, not an error to recover from. Tool
results keep the brain envelope: ``{ok, result}`` or ``{ok: false, error}``
with MCP ``isError`` for logical failures, so revision conflicts stay
structured for the calling agent.
"""
import argparse
import json
import os
import sys

from brain_service.client import Client
from brain_service.policy import API_VERSION, Principal, Invalid as ServiceInvalid, Unavailable as ServiceUnavailable
from brain_service.server import bindings, open_db

PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
NEGOTIATED_VERSION = "2025-03-26"
MAX_FRAME = 65536
SERVER_INFO = {"name": "aeon-brain", "version": "1.0.0"}
DEFAULT_CAPABILITIES = frozenset({"read", "capture", "revise_own", "retract_own", "propose"})


class Invalid(ValueError):
    pass


class Unavailable(RuntimeError):
    pass


def _tools():
    from aeon_sharedwrites.mcp_adapter import general_tools
    return general_tools()


class SocketStore:
    """Proxy mode: no database access; the gateway daemon supplies identity."""

    def __init__(self, socket_path):
        self.client = Client(socket_path)

    def call(self, name, args):
        try:
            return self.client.call(name, args)
        except ServiceInvalid as error:
            raise Invalid("Invalid brain request") from error
        except ServiceUnavailable as error:
            raise Unavailable("Brain service unavailable") from error


class DirectStore:
    """Trusted local mode: this process owns the canonical database."""

    def __init__(self, db, gateway, uid):
        self.gateway, self.uid = gateway, uid

    def call(self, name, args):
        return self.gateway.handle_peer(
            self.uid, dict(api_version=API_VERSION, operation=name, arguments=args))


def _direct_store(db_path, bindings_path, principal_name):
    db = open_db(db_path)
    from brain_service.gateway import Gateway
    from brain_service.service import available
    if not available(db):
        raise Unavailable("Explicit general migration required; refusing to start")
    uid = os.getuid()
    if bindings_path:
        principals, peers = bindings(bindings_path)
        if uid not in peers:
            raise Unavailable("No principal binding for this OS user")
    else:
        name = principal_name
        principal = Principal(name, "local", DEFAULT_CAPABILITIES, "mcp:" + name)
        principals, peers = {name: principal}, {uid: name}
    return db, DirectStore(db, Gateway(db, principals=principals, peer_bindings=peers), uid)


def serve(store, stdin=sys.stdin.buffer, stdout=sys.stdout, *, tool_definitions=_tools):
    """Bounded JSON-RPC 2.0 stdio loop; one framed request per line."""
    initialized = False

    def emit(value):
        stdout.write(json.dumps(value, ensure_ascii=True) + "\n")
        stdout.flush()

    while True:
        raw = stdin.readline(MAX_FRAME + 1)
        if not raw:
            return
        if len(raw) > MAX_FRAME:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Request exceeds limit"}})
            return  # Do not interpret the remainder of an oversized request.
        rid = None
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise Invalid("Invalid JSON-RPC request")
            if "id" not in request:
                continue
            rid = request["id"]
            if type(rid) not in (str, int) or (isinstance(rid, str) and len(rid) > 64):
                rid = None
                raise Invalid("Invalid request ID")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise Invalid("Invalid parameters")
            if method == "initialize":
                version = params.get("protocolVersion")
                if version not in PROTOCOL_VERSIONS:
                    version = NEGOTIATED_VERSION
                result = {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO}
                initialized = True
            elif method == "ping":
                result = {}
            elif not initialized:
                raise Invalid("Initialize first")
            elif method == "tools/list":
                result = {"tools": tool_definitions()}
            elif method == "tools/call":
                value = store.call(params.get("name"), params.get("arguments", {}))
                if not isinstance(value, dict) or type(value.get("ok")) is not bool:
                    raise Unavailable("Malformed brain envelope")
                result = {"content": [{"type": "text", "text": json.dumps(value)}],
                          "structuredContent": value, "isError": value["ok"] is False}
            else:
                emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
                continue
            emit({"jsonrpc": "2.0", "id": rid, "result": result})
        except json.JSONDecodeError:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON"}})
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "Invalid request or tool arguments"}})
        except (Unavailable, ServiceUnavailable):
            emit({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "Brain unavailable"}})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--socket", help="Gateway socket; this process holds no database access")
    mode.add_argument("--db", help="Canonical database; trusted local mode")
    parser.add_argument("--bindings", help="Operator-owned UID bindings file (direct mode)")
    parser.add_argument("--principal-name", default="mcp",
                        help="Inline principal for single-user direct mode (default: mcp)")
    args = parser.parse_args()
    if args.db and args.bindings and not os.path.isfile(args.bindings):
        parser.error("Bindings file not found")
    try:
        if args.socket:
            store = SocketStore(args.socket)
        else:
            _, store = _direct_store(args.db, args.bindings, args.principal_name)
    except (OSError, ServiceInvalid, Unavailable, ServiceUnavailable) as error:
        print(f"aeon-brain: refusing to start: {error}", file=sys.stderr)
        return 1
    serve(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
