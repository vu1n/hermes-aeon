"""Trusted local transport boundary; never accept an actor/owner/capability header."""
import json
import socket
import sqlite3
import struct
from .policy import API_VERSION, Principal, Denied, Invalid, Unavailable
from .service import Service
if '.' in __package__:
    from ..store.shared_writer import WriteError, Conflict
else:
    from store.shared_writer import WriteError, Conflict

class Gateway:
    def __init__(self,db,*,principals,peer_bindings,owner_id='local'):
        self.service=Service(db,owner_id=owner_id)
        self.principals=dict(principals)
        self.peer_bindings=dict(peer_bindings)
        for name,principal in self.principals.items():
            if not isinstance(principal,Principal) or principal.id!=name or principal.owner_id!=owner_id:
                raise Invalid('Invalid principal configuration')
        for uid,name in self.peer_bindings.items():
            if type(uid) is not int or uid<0 or name not in self.principals:raise Invalid('Invalid peer binding')

    def handle_peer(self,peer_uid,request):
        """peer_uid must come from SO_PEERCRED or another trusted transport adapter."""
        try:
            name=self.peer_bindings.get(peer_uid)
            if name is None:raise Denied('Peer unavailable')
            principal=self.principals[name]
            if not isinstance(request,dict) or set(request)!={'api_version','operation','arguments'} or request['api_version']!=API_VERSION:
                raise Invalid('Invalid service request')
            operation,args=request['operation'],request['arguments']
            if not isinstance(args,dict):raise Invalid('Invalid arguments')
            if operation=='aeon_correct':
                args=correction_arguments(args)
                operation='revise'
            aliases={'aeon_capture':'capture','aeon_get':'get','aeon_search':'search','aeon_recent':'recent'}
            operation=aliases.get(operation,operation)
            if operation in {'capture','revise','propose','retract'}:
                if operation=='retract':result=self.service.revise(principal,args,retract=True)
                else:result=getattr(self.service,operation)(principal,args)
            elif operation=='get':
                if set(args)!={'id'}:raise Invalid('Invalid get arguments')
                result={'item':self.service.get(principal,args['id'])}
            elif operation in {'search','recent'}:
                result=getattr(self.service,operation)(principal,**args)
            elif operation in {'status','interests'}:
                if args:raise Invalid('Unexpected arguments')
                result=getattr(self.service,operation)(principal)
            else:raise Invalid('Unknown operation')
            return {'ok':True,'result':result}
        except Denied:return {'ok':False,'error':{'code':'not_found_or_denied'}}
        except (Invalid,ValueError,TypeError,KeyError,RecursionError):return {'ok':False,'error':{'code':'invalid_request'}}
        except Conflict as error:return {'ok':False,'error':{'code':error.code,'current_revision':error.current_revision}}
        except WriteError as error:return {'ok':False,'error':{'code':error.code}}
        except (Unavailable,sqlite3.Error):return {'ok':False,'error':{'code':'general_unavailable'}}

def socket_request(connection,gateway):
    """One bounded Linux Unix request. Connection/DB ownership stays with the server."""
    connection.settimeout(5)
    uid=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
    with connection.makefile('rb') as stream:
        raw=stream.readline(65537)
    if len(raw)>65536 or not raw.endswith(b'\n'):
        return {'ok':False,'error':{'code':'invalid_request'}}
    try:request=json.loads(raw)
    except (ValueError,TypeError,RecursionError):return {'ok':False,'error':{'code':'invalid_request'}}
    return gateway.handle_peer(uid,request)


def correction_arguments(args):
    """Map the existing owned-note correction shape into a typed revision."""
    allowed={'request_id','memory_id','expected_revision','statement','summary','kind','attribution_basis','conversation_ref'}
    if set(args)-allowed:raise Invalid('Unexpected correction fields')
    revision={key:args[key] for key in ['request_id','memory_id','expected_revision','attribution_basis','conversation_ref'] if key in args}
    revision['reason']='Compatibility correction'
    revision['patch']={key:args[key] for key in ['statement','summary','kind'] if key in args}
    return revision
