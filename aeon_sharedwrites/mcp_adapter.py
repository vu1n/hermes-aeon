#!/usr/bin/env python3
"""MCP read/write adapter; canonical DB access belongs to the broker."""
import argparse
import json
import socket
import sys
from pathlib import Path

if __package__:
    from .read_adapter import adapter as reader
else:
    from read_adapter import adapter as reader
    sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

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

class GeneralStore:
    """Explicit service mode: every operation is live; no snapshot or broker fallback."""
    def __init__(self, socket_path):
        from brain_service.client import Client
        self.client=Client(socket_path)

    def call(self,name,args):
        from brain_service.policy import Unavailable as GeneralUnavailable, Invalid as GeneralInvalid
        if name not in {definition['name'] for definition in general_tools()}:
            raise reader.Invalid('Unknown tool')
        try:return self.client.call(name,args)
        except GeneralInvalid as error:raise reader.Invalid('Invalid general request') from error
        except GeneralUnavailable as error:raise reader.Unavailable('General service unavailable') from error


def general_tools():
    """Existing note schemas plus neutral revision/proposal/lifecycle operations."""
    definitions=tools()
    for definition in definitions:
        if definition['name'] in {'aeon_search','aeon_recent','aeon_get'}:
            definition['description']='Read live eligible general memory with provenance; no snapshot fallback.'
    identity={'request_id':{'type':'string','maxLength':120},
              'memory_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},
              'expected_revision':{'type':'integer','minimum':1},
              'reason':{'type':'string','minLength':1,'maxLength':512}}
    patch={'type':'object','minProperties':1,'additionalProperties':False,'properties':{
        'statement':{'type':'string','minLength':1,'maxLength':4000},
        'summary':{'type':'string','maxLength':2000},'title':{'type':'string','maxLength':300},
        'domain':{'type':'string','enum':sorted(reader.DOMAINS)},
        'topics':{'type':'array','maxItems':32,'items':{'type':'string','maxLength':64}},
        'project_id':{'type':'string','maxLength':128},
        'applicability':{'type':'string','maxLength':256},
        'expires_at':{'type':'integer','minimum':1}}}
    for name,extra in [('revise',{'patch':patch}),('propose',{'statement':{'type':'string','minLength':1,'maxLength':4000}}),('retract',{}),('status',{}),('interests',{})]:
        properties={} if name in {'status','interests'} else dict(identity,**extra)
        if name=='propose':properties['reason']={'type':'string','minLength':1,'maxLength':256}
        definitions.append({'name':name,'description':'General memory '+name+'. Identity is server-controlled; attribution is a client claim.',
            'inputSchema':{'type':'object','properties':properties,'required':list(properties),'additionalProperties':False},
            'annotations':{'readOnlyHint':name in {'status','interests'},'destructiveHint':name=='retract','idempotentHint':True,'openWorldHint':False}})
    for definition in definitions:
        properties=definition['inputSchema']['properties']
        if definition['name']=='aeon_capture':
            properties.update(topics={'type':'array','maxItems':32,'items':{'type':'string','maxLength':64}},
                              record_class={'type':'string','enum':['assertion','working_context']},
                              project_id={'type':'string','maxLength':128},
                              expires_at={'type':'integer','minimum':1},
                              applicability={'type':'string','maxLength':256})
        prop=properties.get('request_id')
        if prop:prop.update(pattern='^[A-Za-z0-9_.:-]{1,120}$',maxLength=120)
    source_refs={'type':'array','minItems':1,'maxItems':16,'items':{'type':'object','additionalProperties':False,
        'properties':{'memory_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},'revision':{'type':'integer','minimum':1}},
        'required':['memory_id','revision']}}
    for name in ['consolidate_preview','consolidate_stage']:
        properties={'source_refs':source_refs}
        if name=='consolidate_stage':properties['expected_candidate_id']={'type':'string','pattern':'^[a-f0-9]{64}$'}
        definitions.append({'name':name,'description':'Preview or stage a source-linked candidate for owner review. Never applies corrections or promotes preferences.',
            'inputSchema':{'type':'object','properties':properties,'required':list(properties),'additionalProperties':False},
            'annotations':{'readOnlyHint':name=='consolidate_preview','destructiveHint':False,'idempotentHint':True,'openWorldHint':False}})
    aid={'type':'string','pattern':'^[a-f0-9]{32}$'}
    review_schemas={
        'review_stage':dict(request_id={'type':'string','maxLength':120},source_refs=source_refs,
            expected_candidate_id={'type':'string','pattern':'^[a-f0-9]{64}$'},parent_attempt=aid),
        'review_pending':dict(limit={'type':'integer','minimum':1,'maximum':50},after=aid),
        'review_history':dict(attempt_id=aid),
        'review_decide':dict(request_id={'type':'string','maxLength':120},attempt_id=aid,expected_revision={'type':'integer','minimum':1},
            decision={'type':'string','enum':['accepted','rejected']},reason_code={'type':'string','enum':['faithful','conflict','outdated','duplicate','irrelevant']})}
    for name,properties in review_schemas.items():
        required=[] if name=='review_pending' else [key for key in properties if key!='parent_attempt']
        definitions.append({'name':name,'description':'Durable review metadata; acceptance records a decision only, never promotes preferences or applies corrections.',
            'inputSchema':{'type':'object','properties':properties,'required':required,'additionalProperties':False},
            'annotations':{'readOnlyHint':name in {'review_pending','review_history'},'destructiveHint':False,'idempotentHint':True,'openWorldHint':False}})
    return definitions


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db')
    parser.add_argument('--broker-socket')
    parser.add_argument('--general-socket',help='Opt in to live brain.general.v1; forbids projection/broker arguments')
    args=parser.parse_args()
    if args.general_socket:
        if args.db or args.broker_socket:parser.error('General mode cannot use a projection or legacy broker')
        reader.serve(GeneralStore(args.general_socket),tool_definitions=general_tools)
    else:
        if not args.db or not args.broker_socket:parser.error('Legacy mode requires --db and --broker-socket')
        reader.serve(Store(args.db,args.broker_socket), tool_definitions=tools)

if __name__=='__main__': main()
