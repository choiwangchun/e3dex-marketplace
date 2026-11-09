"""Local E3D session discovery, guarded macro runner and newline-JSON MCP server."""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from e3d_bridge.errors import BridgeError, InputError

REGISTRY = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'AVEVA/E3DBridge/sessions'
HARNESS = Path(__file__).resolve().parents[2] / 'e3d_harness.py'
SESSION_PATTERN = re.compile(r'(?:des|draw)-[0-9]+-[0-9]+\Z')


class ProjectNotAllowed(BridgeError):
    def __init__(self, message):
        super().__init__('PROJECT_NOT_ALLOWED', message)


def require_allowed_project(project):
    from e3d_bridge.target import project_name
    project_name(project)


def process_filetime(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        raise BridgeError('SESSION_UNAVAILABLE', 'E3D process has exited or cannot be inspected')
    try:
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            raise ctypes.WinError(ctypes.get_last_error())
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def load_session(session_id, registry=REGISTRY):
    if not SESSION_PATTERN.fullmatch(session_id):
        raise InputError('INVALID_REQUEST', 'Use an exact session_id returned by e3d_list_sessions')
    try:
        record = json.loads((registry / (session_id + '.json')).read_text(encoding='utf-8-sig'))
    except FileNotFoundError as exc:
        raise BridgeError('SESSION_NOT_FOUND', 'Session registration was not found') from exc
    except (ValueError, OSError) as exc:
        raise BridgeError('INVALID_SESSION', 'Cannot read session registration') from exc
    required = ('session_id', 'pid', 'process_start_filetime', 'http_url', 'module')
    if not isinstance(record, dict) or any(k not in record for k in required):
        raise BridgeError('INVALID_SESSION', 'Incomplete session registration')
    if record['session_id'] != session_id or str(record['pid']) != session_id.split('-')[1]:
        raise InputError('INVALID_SESSION', 'Session registration identity mismatch')
    actual_start = process_filetime(record['pid'])
    if str(actual_start) != record['process_start_filetime'] or str(actual_start) != session_id.split('-')[2]:
        raise BridgeError('STALE_SESSION', 'Stale session registration; PID belongs to another process')
    if not re.fullmatch(r'http://127\.0\.0\.1:188[0-9][02468]', record['http_url']):
        raise InputError('INVALID_SESSION', 'Session endpoint is outside the local bridge port range')
    if record.get('token_required'):
        raise BridgeError('SESSION_AUTH_UNSUPPORTED', 'This session requires an E3D-side token; it is not supported by the session backend')
    if record.get('engine') != 'e3d-native':
        raise BridgeError('LEGACY_BRIDGE_UNSUPPORTED', 'Version 3 requires EhNativeBridge; install/start the independent bridge in the selected E3D window')
    return record


def verify_listener_owner(record):
    """The companion WS socket identifies which process owns this HTTP bridge pair."""
    class Row(ctypes.Structure):
        _fields_ = [(name, wintypes.DWORD) for name in
                    ('state', 'local_address', 'local_port', 'remote_address', 'remote_port', 'pid')]
    api = ctypes.WinDLL('iphlpapi', use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                    wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
    api.restype = wintypes.DWORD
    size = wintypes.DWORD()
    status = api(None, ctypes.byref(size), False, socket.AF_INET, 3, 0)
    if status not in (0, 122):
        raise OSError(status, 'Cannot inspect bridge listener ownership')
    # The table may grow between sizing and reading it.
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        status = api(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        if status != 122:
            break
    if status:
        raise OSError(status, 'Cannot inspect bridge listener ownership')
    count = wintypes.DWORD.from_buffer(buffer).value
    http_port = int(record['http_url'].rsplit(':', 1)[1])
    expected_port = http_port if record.get('engine') == 'e3d-native' else http_port + 1
    for i in range(count):
        row = Row.from_buffer(buffer, ctypes.sizeof(wintypes.DWORD) + i * ctypes.sizeof(Row))
        if socket.ntohs(row.local_port & 0xffff) == expected_port:
            if row.pid == record['pid']:
                return
            raise BridgeError('LISTENER_MISMATCH', 'Bridge port now belongs to another process; refresh session list')
    raise BridgeError('SESSION_UNAVAILABLE', 'The selected E3DEX listener is no longer running')


def verify_process(record):
    if str(process_filetime(record['pid'])) != str(record['process_start_filetime']):
        raise BridgeError('STALE_SESSION', 'Stale session registration; PID belongs to another process')


def get_context(record, timeout=4):
    verify_listener_owner(record)
    verify_process(record)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(record['http_url'] + '/context', timeout=timeout) as response:
            data = json.loads(response.read().decode('utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise BridgeError('BRIDGE_UNAVAILABLE', 'Context request failed: ' + str(exc)) from exc
    if data.get('ok') is not True:
        raise BridgeError('BRIDGE_ERROR', 'Context request failed: ' + str(data.get('error', 'unknown error')))
    if data['data'].get('module') != record['module']:
        raise BridgeError('SESSION_MISMATCH', 'Bridge module does not match the session registration')
    require_allowed_project(data['data'].get('project', {}).get('name'))
    if record.get('engine') == 'e3d-native':
        native = data['data'].get('bridge', {})
        if native.get('engine') != 'e3d-native' or native.get('session_id') != record['session_id']:
            raise BridgeError('SESSION_MISMATCH', 'Independent bridge identity does not match registration')
    if not data['data'].get('project', {}).get('loggedIn') or not data['data'].get('currentElement', {}).get('isValid'):
        raise BridgeError('INVALID_CONTEXT', 'E3D must be logged in with a valid current element')
    verify_listener_owner(record)
    verify_process(record)
    return data['data']


def session_context(session_id):
    record = load_session(session_id)
    return {'session': record, 'context': get_context(record)}


def list_sessions(registry=REGISTRY, project=None):
    if project is not None: require_allowed_project(project)
    def inspect(path):
        try:
            record = load_session(path.stem, registry)
            context = get_context(record)
            if project is not None and context['project']['name'] != project: return None
            return {'session': record, 'context': context, 'available': True}
        except ProjectNotAllowed:
            return None
        except Exception as exc:
            return {'session_id': path.stem, 'available': False, 'reason': str(exc),
                    'error': {'code': getattr(exc, 'code', 'INVALID_SESSION'), 'message': str(exc),
                              'details': getattr(exc, 'details', {}), 'retryable': False}}
    paths = sorted(registry.glob('*.json')) if registry.exists() else []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(inspect, paths))
    return {'project_filter': project, 'sessions': [r for r in results if r is not None],
            'selection': 'Show open projects and ask the user where to work. Reuse explicit user choice; never select the first session implicitly. Pass the exact session_id for every request.'}


@contextlib.contextmanager
def session_lock(session_id):
    import msvcrt
    folder = REGISTRY.parent / 'locks'
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / (session_id + '.lock')).open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BridgeError('SESSION_BUSY', 'This E3D session is busy in another multi-session client') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def run_macro(args):
    if args.get('confirm') is not True:
        raise InputError('CONFIRMATION_REQUIRED', 'Macro execution requires confirm=true')
    require_allowed_project(args['project'])
    record = load_session(args['session_id'])
    macro = Path(args['macro'])
    if not macro.is_absolute() or macro.suffix.lower() != '.pmlmac' or not macro.is_file():
        raise InputError('INVALID_MACRO', 'macro must be an existing absolute .pmlmac path')
    if args['effect'] not in ('read-only', 'session', 'model'):
        raise InputError('INVALID_REQUEST', 'Invalid effect')
    from e3d_bridge.target import check_macro_target
    check_macro_target(macro, args)
    with session_lock(args['session_id']):
        record = load_session(args['session_id'])
        context = get_context(record)
        expected = (args['project'], args['mdb'].lstrip('/'), args['ref'])
        actual = (context['project']['name'], context['mdb']['name'].lstrip('/'), context['currentElement']['ref'])
        if actual != expected:
            raise InputError('TARGET_CHANGED', 'Target changed; request fresh context before executing a macro')
        if args['effect'] == 'model' and context['currentElement'].get('readOnly') is not False:
            raise BridgeError('READ_ONLY', 'Model effects require a writable current element')
        command = [sys.executable, str(HARNESS), '--url', record['http_url'], '--session-id', args['session_id'],
                   'run', str(macro), '--effect', args['effect'], '--project', args['project'],
                   '--mdb', context['mdb']['name'], '--ref', args['ref']]
        for expectation in args.get('expect', []):
            command.extend(['--expect', expectation])
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=180, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
        except subprocess.TimeoutExpired as exc:
            raise BridgeError('INDETERMINATE', 'Timed out; do not repeat the macro without inspecting session state and harness runs',
                              {'inspect_report_before_retry': True, 'runs': str(Path(os.environ['E3D_BRIDGE_DATA_DIR']) / 'runs' if os.environ.get('E3D_BRIDGE_DATA_DIR') else HARNESS.parent / 'runs')}) from exc
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = {'stdout': result.stdout, 'stderr': result.stderr}
        return {'session_id': args['session_id'], 'ok': result.returncode == 0, 'exit_code': result.returncode, 'report': report}


# Compatibility entry points use the same operation catalog and adapters.
from e3d_bridge.contract import tools
TOOLS = tools()


def rpc(message):
    from e3d_bridge.mcp import rpc as dispatch
    return dispatch(message)


def main():
    from e3d_bridge.cli import main as entry
    return entry()


if __name__ == '__main__':
    raise SystemExit(main())
