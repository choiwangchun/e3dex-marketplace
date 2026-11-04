"""Command line: `mcp` (Codex / Claude Code), `login`, `logout`, `account`, or any app command after the online check."""
import json
import sys
import uuid

from . import LOADER_VERSION, LoaderError, ROOT, data_dir, load_config, package_version, version_key
from .account import Account
from .client import Http


def envelope(operation, exc):
    return {'schema_version': '1.0', 'request_id': uuid.uuid4().hex, 'operation': operation, 'session_id': None,
            'ok': False, 'data': None,
            'error': {'code': exc.code, 'message': str(exc), 'details': exc.details, 'retryable': False}}


def emit(value):
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8') + b'\n')
    sys.stdout.flush()


def main(argv):
    command = argv[0] if argv else 'status'
    try:
        config = load_config()
        folder = data_dir()
        if command == 'mcp':
            from .proxy import Proxy
            Proxy(config, folder).serve()
            return 0
        account = Account(config, folder, Http(config['server_url']))
        if command == 'login':
            def show(pending):
                print('브라우저에서 아래 주소를 열고 코드 %s 를 확인하세요:\n  %s' % (
                    pending['user_code'], pending['verification_uri_complete']), file=sys.stderr, flush=True)
            result = account.login(wait_seconds=900, on_start=show)
            emit(result)
            return 0 if result['status'] == 'LOGGED_IN' else 2
        if command == 'logout':
            emit(account.logout())
            return 0
        if command == 'configure':
            raise LoaderError('CONFIG_ERROR', 'Install this package through the plugin marketplace; do not add a duplicate MCP config.')
        session, _ = account.session(package_version())
        if version_key(LOADER_VERSION) < version_key(session.get('min_loader_version') or '0'):
            raise LoaderError('LOADER_OUTDATED', '마켓플레이스에서 E3DEX 플러그인을 최신 버전으로 업데이트하세요.')
        if command == 'account':
            emit({'status': 'LOGGED_IN', 'user': session['user'], 'device': session['device'],
                  'app_version': (session.get('app') or {}).get('version'), 'plugin_version': package_version(),
                  'server': config['server_url']})
            return 0
        if command == 'check':
            sys.path.insert(0, str(ROOT / 'scripts'))
            from check import main as check
            return check(argv[1:])
        from e3d_bridge.cli import main as app_main
        return app_main(argv)
    except LoaderError as exc:
        emit(envelope(command, exc))
        return 2
