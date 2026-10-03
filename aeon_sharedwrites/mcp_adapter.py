#!/usr/bin/env python3
"""MCP read/write adapter; canonical DB access belongs to the broker."""
import argparse
import json
import socket

if __package__:
    from .read_adapter import adapter as reader
else:
    from read_adapter import adapter as reader

READ_TOOLS = reader.tools

def tools():
    common = {
        'request_id': {'type':'string','pattern':'^[A-Za-z0-9_.:-]{1,128}$'},
        'kind': {'type':'string','enum':['idea','decision','preference','project_context']},
        'attribution_basis': {'type':'string','enum':['user_explicit','assistant_inferred','user_corrected']},
        'conversation_ref': {'type':'string','maxLength':128},
        'summary': {'type':'string','maxLength':1000},
        'statement': {'type':'string','minLength':1,'maxLength':4000},
    }
    definitions = READ_TOOLS()
    for name, extra, required in [
        ('aeon_capture', {'domain':{'type':'string','enum':sorted(reader.DOMAINS)},'title':{'type':'string','maxLength':300}}, ['domain']),
        ('aeon_correct', {'memory_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},'expected_revision':{'type':'integer','minimum':1}}, ['memory_id','expected_revision']),
    ]:
        definitions.append({'name':name,'description':'Store a concise shared note or append its correction. Attribution is caller-reported. Reuse request_id only for retries of the identical event. No health, sensitive finances, secrets, URLs or transcripts.',
            'inputSchema':{'type':'object','properties':dict(common,**extra),'required':['request_id','kind','attribution_basis','statement']+required,'additionalProperties':False},
            'annotations':{'readOnlyHint':False,'destructiveHint':False,'idempotentHint':True,'openWorldHint':False}})
    return definitions

class Store(reader.Store):
    def __init__(self, db, broker_socket):
        super().__init__(db)
        self.broker_socket = broker_socket

    @classmethod
    def open_projection(cls, path):
        # Refresh only the read connection; broker identity/socket stay on this instance.
        return reader.Store(path)

    def call(self, name, args):
        if name=='aeon_get':
            reader.arguments(args,{'id'})
            fresh=self.broker_call('get',args)
            if not fresh['ok']:raise reader.Unavailable('Canonical owned-note read unavailable')
            if fresh['result']['owned']:return {'item':fresh['result']['item']}
            return super().call(name,args)
        if name not in {'aeon_capture','aeon_correct'}:
            return super().call(name,args)
        if not isinstance(args,dict): raise reader.Invalid('Expected arguments')
        return self.broker_call('capture' if name=='aeon_capture' else 'update',args)

    def broker_call(self,operation,args):
        payload=(json.dumps({'operation':operation,'arguments':args})+'\n').encode()
        if len(payload)>65536: raise reader.Invalid('Request exceeds limit')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(self.broker_socket)
            connection.sendall(payload)
            with connection.makefile('rb') as response:
                raw=response.readline(65537)
        if len(raw)>65536 or not raw.endswith(b'\n'): raise reader.Unavailable('Invalid broker response')
        value=json.loads(raw)
        if not isinstance(value,dict) or type(value.get('ok')) is not bool:
            raise reader.Unavailable('Invalid broker response')
        # Logical failures remain structured so callers can resolve revision conflicts.
        return value

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--broker-socket',required=True)
    args=parser.parse_args()
    reader.serve(Store(args.db,args.broker_socket), tool_definitions=tools)

if __name__=='__main__': main()
