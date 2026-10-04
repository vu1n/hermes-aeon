"""Agent-neutral general API client. Holds a socket path, never a database or identity."""
import argparse
import json
import socket
from .policy import API_VERSION, Invalid, Unavailable

MAX_FRAME=65536

class Client:
    def __init__(self, socket_path: str) -> None:
        self.socket_path=socket_path

    def call(self, operation: str, arguments: dict[str, object]) -> dict[str, object]:
        if not isinstance(operation,str) or not isinstance(arguments,dict):
            raise Invalid('Invalid request')
        try:
            payload=(json.dumps(dict(api_version=API_VERSION,operation=operation,arguments=arguments),allow_nan=False)+'\n').encode()
        except (ValueError,TypeError,RecursionError) as error:
            raise Invalid('Invalid request') from error
        if len(payload)>MAX_FRAME:raise Invalid('Request exceeds limit')
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(5)
                connection.connect(self.socket_path)
                connection.sendall(payload)
                with connection.makefile('rb') as stream:
                    raw=stream.readline(MAX_FRAME+1)
            if len(raw)>MAX_FRAME or not raw.endswith(b'\n'):raise Unavailable('Invalid service response')
            value=json.loads(raw)
            if not isinstance(value,dict) or type(value.get('ok')) is not bool:
                raise Unavailable('Invalid service response')
            if value['ok']:
                if not isinstance(value.get('result'),dict):raise Unavailable('Invalid service response')
            elif not isinstance(value.get('error'),dict) or not isinstance(value['error'].get('code'),str):
                raise Unavailable('Invalid service response')
            return value
        except (OSError,ValueError,RecursionError) as error:
            raise Unavailable('General service unavailable') from error


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket',required=True)
    parser.add_argument('operation')
    parser.add_argument('arguments',help='JSON object; actor/source/owner are server-controlled')
    args=parser.parse_args()
    try:
        result=Client(args.socket).call(args.operation,json.loads(args.arguments))
    except (Invalid,ValueError):
        result={'ok':False,'error':{'code':'invalid_request'}}
    except Unavailable:
        result={'ok':False,'error':{'code':'general_unavailable'}}
    print(json.dumps(result))
    return 0 if result['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
