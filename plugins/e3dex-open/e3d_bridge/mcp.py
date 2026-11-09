"""MCP stdio adapter; all operations go through Service.call."""
import json
import sys
from . import __version__
from .contract import tools
from .errors import BridgeError
from .service import Service, parse_json


def rpc(message, service=None):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    method = message['method']
    if 'id' not in message:
        return None
    reply = {'jsonrpc': '2.0', 'id': message['id']}
    params = message.get('params', {})
    if not isinstance(params, dict):
        return {**reply, 'error': {'code': -32602, 'message': 'params must be an object'}}
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion', '2025-06-18'),
                  'capabilities': {'tools': {'listChanged': False}},
                  'serverInfo': {'name': 'e3dex', 'version': __version__}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': tools()}
    elif method in ('resources/list', 'prompts/list'):
        result = {method.split('/')[0]: []}
    elif method == 'tools/call':
        request = {'operation': params.get('name'), 'arguments': params.get('arguments', {})}
        meta = params.get('_meta', {})
        if isinstance(meta, dict) and 'request_id' in meta:
            request['request_id'] = meta['request_id']
        payload = (service or Service()).call(request)
        result = {'content': [{'type': 'text', 'text': json.dumps(payload, ensure_ascii=False)}],
                  'structuredContent': payload, 'isError': not payload['ok']}
    else:
        return {**reply, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {**reply, 'result': result}


def serve():
    service = Service()
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        try:
            response = rpc(parse_json(line), service)
        except BridgeError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if response is not None:
            sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode('utf-8') + b'\n')
            sys.stdout.buffer.flush()
