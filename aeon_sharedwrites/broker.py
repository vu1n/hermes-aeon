#!/usr/bin/env python3
"""Local Unix-socket writer. Installing this code alone grants no access.

Peer UID determines the consumer; clients cannot choose source/actor. No URL
extraction, embeddings, network egress or transcript uploads.
"""
import argparse
import importlib.util
import json
import os
import re
import socket
import socketserver
import sqlite3
import struct
import threading
import time
from pathlib import Path

try:
    from . import shared_writer as writer
except ImportError:
    writer = None  # Direct CLI loads the explicit root-controlled module in main.

DOMAINS = {'work','side_projects','learning'}
KINDS = {'idea','decision','preference','project_context'}
BASES = {'user_explicit','assistant_inferred','user_corrected'}
SENSITIVE = re.compile(r'(?i)\b(?:health|medical|patient|diagnos\w*|therapy|sleep|nutrition|oura|multivitamin|medication|prescription|salary|bank|mortgage|credit\s*card|investment|financial|password|credential|secret|bearer|private[ _-]?key)\b|api[ _-]?key|\bsk-[A-Za-z0-9_-]+|\btoken\s*[:=]|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|https?://')

def validate(args, operation):
    allowed={'request_id','domain','kind','attribution_basis','conversation_ref','title','summary','statement'}
    if operation=='update':allowed={'request_id','kind','attribution_basis','conversation_ref','summary','statement','memory_id','expected_revision'}
    if not isinstance(args,dict) or set(args)-allowed:raise writer.Invalid()
    if operation not in {'capture','update'}:raise writer.Invalid()
    if operation=='capture' and args.get('domain') not in DOMAINS:raise writer.Invalid()
    if args.get('kind') not in KINDS or args.get('attribution_basis') not in BASES:raise writer.Invalid()
    statement=args.get('statement')
    if not isinstance(statement,str) or not 1<=len(statement.strip())<=4000:raise writer.Invalid()
    for field,limit in [('title',300),('summary',1000),('conversation_ref',128)]:writer.text(args.get(field),limit)
    if SENSITIVE.search('\n'.join(args.get(k) or '' for k in ['title','summary','statement','conversation_ref'])):raise writer.Invalid()
    writer.identity(args.get('request_id'))

def current_note(db, consumer, args):
    """Immediate owned-note read, using the SAME screening as the projection."""
    if not isinstance(args,dict) or set(args)!={'id'} or not isinstance(args['id'],str) or not re.fullmatch('[a-f0-9]{32}',args['id']):
        raise writer.Invalid()
    spec=importlib.util.spec_from_file_location('broker_projection_rules',Path(__file__).with_name('read_adapter')/'adapter.py')
    rules=importlib.util.module_from_spec(spec);spec.loader.exec_module(rules)
    fields=tuple(k for k in rules.FIELDS if k not in {'provenance_json','screened_at','scope_version'})
    db.execute('BEGIN')
    try:
        raw=db.execute('SELECT '+','.join(fields)+' FROM memory_items WHERE id=? AND source=?',(args['id'],'chat:'+consumer)).fetchone()
        if not raw:return {'item':None,'owned':False}
        row=dict(zip(fields,raw))
        audit=db.execute('SELECT consumer_id,entry_kind,attribution_basis,conversation_ref,created_at FROM memory_write_audit WHERE memory_id=? AND revision_n=?',(row['id'],row['current_revision'])).fetchone()
        if audit is None:return {'item':None,'owned':True}
        row.update(screened_at=int(time.time()*1000),scope_version=rules.SCOPE,
            provenance_json=json.dumps(dict(consumer_id=audit[0],kind=audit[1],attribution_basis=audit[2],conversation_ref=audit[3],created_at=audit[4],revision=row['current_revision'])))
        return {'item':rules.Store.safe(row,full=True),'owned':True}
    finally:
        db.execute('ROLLBACK')

def operation(db, consumer, request):
    if not isinstance(request,dict) or set(request)!={'operation','arguments'}:raise writer.Invalid()
    op,args=request['operation'],request['arguments']
    if op=='get':return current_note(db,consumer,args)
    validate(args,op)
    meta=dict(consumer_id=consumer,request_id=args['request_id'],entry_kind=args['kind'],
              attribution_basis=args['attribution_basis'],conversation_ref=args.get('conversation_ref'))
    source='chat:'+consumer
    if op=='capture':
        return writer.capture(db,type='note',domain=args['domain'],title=args.get('title'),
            content=args['statement'],summary=args.get('summary'),source=source,
            tags=['chat:'+args['kind'],'attribution:'+args['attribution_basis']],**meta)
    return writer.update(db,memory_id=args.get('memory_id'),expected_revision=args.get('expected_revision'),
        content=args['statement'],summary=args.get('summary'),source=source,
        eligible_sources={source},eligible_domains=DOMAINS,**meta)

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        uid=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
        consumer=self.server.consumers.get(uid)
        if consumer is None:
            self.reply({'ok':False,'error':{'code':'unauthorized_peer'}});return
        db=None
        try:
            raw=self.rfile.readline(65537)
            if len(raw)>65536 or not raw.endswith(b'\n'):raise writer.Invalid()
            request=json.loads(raw)
            db=sqlite3.connect(self.server.db_path,timeout=.1,isolation_level=None)
            db.execute('PRAGMA foreign_keys=ON');db.execute('PRAGMA trusted_schema=OFF')
            result=operation(db,consumer,request)
            self.reply({'ok':True,'result':result})
        except writer.WriteError as error:
            info={'code':error.code}
            if isinstance(error,writer.Conflict):info['current_revision']=error.current_revision
            self.reply({'ok':False,'error':info})
        except (ValueError,TypeError,KeyError,sqlite3.Error,OSError):
            self.reply({'ok':False,'error':{'code':'write_unavailable'}})
        finally:
            if db is not None:db.close()
    def reply(self,value):
        self.wfile.write((json.dumps(value)+'\n').encode())

class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True
    def __init__(self,path,db_path,consumers,listen_fd=None):
        self.db_path=str(Path(db_path).resolve(strict=True))
        self.consumers=consumers
        self.slots=threading.BoundedSemaphore(4)
        super().__init__(path,Handler,bind_and_activate=listen_fd is None)
        if listen_fd is not None:
            self.socket.close();self.socket=socket.socket(fileno=listen_fd)
        else:os.chmod(path,0o600)  # Test/local mode; systemd owns production socket.
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            request.sendall(b'{"ok":false,"error":{"code":"write_busy"}}\n');self.shutdown_request(request);return
        try:super().process_request(request,address)
        except Exception:self.slots.release();raise
    def process_request_thread(self,request,address):
        try:super().process_request_thread(request,address)
        finally:self.slots.release()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True)
    p.add_argument('--socket',default='/run/aeon-write/broker.sock')
    p.add_argument('--listen-fd',type=int)
    p.add_argument('--allowed-uid',type=int,required=True)
    p.add_argument('--consumer-id',required=True)
    p.add_argument('--shared-module',required=True,help='Exact shared module installed in Hermes store')
    args=p.parse_args()
    # Load only the explicit root-controlled common module, not Hermes providers.
    module=Path(args.shared_module).resolve(strict=True)
    for path in [module,*module.parents]:
        if path.stat().st_uid!=0 or path.stat().st_mode&0o022:raise SystemExit('Untrusted shared module path')
    global writer
    spec=importlib.util.spec_from_file_location('aeon_canonical_writer',module)
    writer=importlib.util.module_from_spec(spec);spec.loader.exec_module(writer)
    writer.identity(args.consumer_id)
    with Server(args.socket,args.db,{args.allowed_uid:args.consumer_id},args.listen_fd) as server:
        server.serve_forever(poll_interval=.25)

if __name__=='__main__':main()
