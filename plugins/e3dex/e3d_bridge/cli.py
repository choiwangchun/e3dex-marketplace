"""CLI adapter with the same request object and JSON result as HTTP/MCP."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
from .contract import catalog
from .errors import BridgeError, InputError
from .service import Service, ROOT, envelope, parse_json


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError('INVALID_REQUEST', message)


def parser():
    p = Parser(description='Unified E3DEX: explicit project/work DB selection, MCP, JSON API, CLI')
    sub = p.add_subparsers(dest='action', required=True)
    for name in ('status', 'list', 'mcp', 'schema', 'configure'):
        sub.add_parser(name)
    context = sub.add_parser('context')
    context.add_argument('session_id')
    context.add_argument('--request-id')
    setup = sub.add_parser('setup')
    setup.add_argument('setup_action', choices=['inspect', 'apply', 'verify'])
    setup.add_argument('--module', choices=['Design', 'Draw', 'Both'], default='Design')
    setup.add_argument('--e3d-root')
    setup.add_argument('--session-id')
    setup.add_argument('--project')
    setup.add_argument('--confirm', action='store_true')
    setup.add_argument('--allow-elevation', action='store_true')
    run = sub.add_parser('run')
    run.add_argument('session_id')
    run.add_argument('macro')
    for field in ('project', 'mdb', 'ref', 'effect'):
        run.add_argument('--' + field, required=True)
    run.add_argument('--confirm', action='store_true')
    run.add_argument('--expect', action='append', default=[])
    run.add_argument('--request-id')
    call = sub.add_parser('call')
    source = call.add_mutually_exclusive_group(required=True)
    source.add_argument('--json')
    source.add_argument('--input', help='UTF-8 JSON file, or - for stdin')
    api = sub.add_parser('serve')
    api.add_argument('--port', type=int)
    return p


def main(argv=None):
    request = None
    try:
        args = parser().parse_args(argv if argv is not None else (sys.argv[1:] or ['status']))
        if args.action == 'mcp':
            from .mcp import serve
            serve()
            return 0
        if args.action == 'serve':
            from .http_api import serve
            serve(args.port)
            return 0
        if args.action == 'configure':
            sys.path.insert(0, str(ROOT / 'scripts'))
            from configure import configure
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                configure(ROOT)
            result = envelope(data=json.loads(output.getvalue()))
        elif args.action == 'schema':
            result = envelope(data=catalog())
        elif args.action == 'setup':
            arguments = {k: v for k, v in vars(args).items() if v is not None and k != 'action'}
            arguments['action'] = arguments.pop('setup_action')
            request = {'operation': 'e3d_setup', 'arguments': arguments}
            result = Service().call(request)
        else:
            if args.action == 'call':
                raw = args.json if args.json is not None else (
                    sys.stdin.buffer.read() if args.input == '-' else Path(args.input).read_text(encoding='utf-8-sig'))
                request = parse_json(raw)
            else:
                arguments = vars(args).copy()
                action = arguments.pop('action')
                rid = arguments.pop('request_id', None)
                request = {'operation': {'status': 'e3d_list_sessions', 'list': 'e3d_list_sessions',
                                        'context': 'e3d_session_context', 'run': 'e3d_run_macro'}[action],
                           'arguments': arguments}
                if rid is not None:
                    request['request_id'] = rid
            result = Service().call(request)
    except BridgeError as exc:
        result = envelope(request, error=exc)
    except (OSError, ValueError) as exc:
        result = envelope(request, error=BridgeError('CONFIG_ERROR', str(exc)))
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8') + b'\n')
    return 0 if result['ok'] else 2
