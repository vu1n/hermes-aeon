"""Explicit Linux local gateway; no schema bootstrap, network listener or Hermes runtime."""
from contextlib import closing
import argparse
import json
import os
import socket
import socketserver
import sqlite3
from pathlib import Path
from .gateway import Gateway, socket_request
from .policy import Principal, Invalid
from .service import available
from .client import MAX_FRAME

CAPABILITIES=frozenset({'read','capture','revise_own','retract_own','propose','import','derive','review'})

def bindings(path):
    config=json.loads(Path(path).read_text())
    if not isinstance(config,list) or not config:raise Invalid('Expected principal bindings')
    principals: dict[str, Principal]={}
    peers: dict[int, str]={}
    for entry in config:
        if not isinstance(entry,dict) or set(entry)!={'uid','id','source_namespace','capabilities'}:
            raise Invalid('Invalid binding')
        uid,name,source,caps=(entry[k] for k in ['uid','id','source_namespace','capabilities'])
        if type(uid) is not int or uid<0 or uid in peers or not isinstance(name,str) or not name or name in principals:
            raise Invalid('Duplicate or invalid identity')
        if not isinstance(source,str) or not source or len(source)>128 or not isinstance(caps,list) or any(not isinstance(c,str) or c not in CAPABILITIES for c in caps):
            raise Invalid('Invalid principal')
        if any(p.source_namespace==source for p in principals.values()):raise Invalid('Duplicate source namespace')
        principals[name]=Principal(name,'local',frozenset(caps),source)
        peers[uid]=name
    return principals,peers


def open_db(path):
    db=sqlite3.connect(Path(path).resolve(strict=True).as_uri()+'?mode=rw',uri=True,timeout=1,isolation_level=None)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA trusted_schema=OFF')
    return db

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with closing(open_db(self.server.db_path)) as db:
                gateway=Gateway(db,principals=self.server.principals,peer_bindings=self.server.peers)
                result=socket_request(self.request,gateway)
                raw=(json.dumps(result)+'\n').encode()
                if len(raw)>MAX_FRAME:
                    raw=b'{"ok":false,"error":{"code":"general_unavailable"}}\n'
                self.request.sendall(raw)
        except (OSError,sqlite3.Error,ValueError):
            self.request.sendall(b'{"ok":false,"error":{"code":"general_unavailable"}}\n')

class Server(socketserver.UnixStreamServer):
    def __init__(self,path,db_path,principals,peers):
        if not hasattr(socket,'SO_PEERCRED'):raise Invalid('Linux peer credentials required')
        self.db_path=str(Path(db_path).resolve(strict=True))
        self.principals,self.peers=principals,peers
        with closing(open_db(self.db_path)) as db:
            if not available(db):raise Invalid('Explicit general migration required')
            Gateway(db,principals=principals,peer_bindings=peers)
        # Never unlink an existing endpoint; operators must resolve stale sockets.
        previous=os.umask(0o177)
        try:super().__init__(path,Handler)
        finally:os.umask(previous)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--socket',required=True)
    parser.add_argument('--bindings',required=True,help='Trusted operator-owned JSON, never client input')
    args=parser.parse_args()
    principals,peers=bindings(args.bindings)
    with Server(args.socket,args.db,principals,peers) as server:
        try:server.serve_forever(poll_interval=.25)
        finally:os.unlink(args.socket)

if __name__=='__main__':main()
