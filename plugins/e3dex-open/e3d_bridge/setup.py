"""Initial setup uses the same service contract on MCP, HTTP and CLI."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from .errors import BridgeError
from .service import ROOT


def scripts():
    directory = ROOT / 'scripts'
    if not (directory / 'setup.ps1').is_file():
        directory = ROOT / 'packaging/plugin/scripts'
    return directory


def installation(args):
    command = ['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
               '-ExecutionPolicy', 'Bypass', '-File', str(scripts() / 'setup.ps1'),
               '-Action', args['action'], '-Module', args.get('module', 'Design')]
    if args.get('e3d_root'):
        command += ['-E3DRoot', args['e3d_root']]
    if args.get('allow_elevation'):
        command += ['-Elevate']
    try:
        result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=100)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError('SETUP_INDETERMINATE', 'Setup did not finish. Inspect installation files, backups and existing elevated process before retrying.') from exc
    try:
        data = json.loads(result.stdout.lstrip('\ufeff'))
    except ValueError as exc:
        raise BridgeError('SETUP_FAILED', 'Setup returned no valid JSON.', {'stderr': result.stderr[-4000:]}) from exc
    if result.returncode or data.get('status') == 'FAILED':
        raise BridgeError('SETUP_FAILED', data.get('error', 'Setup failed'), data)
    return data


def verify(args, service):
    listing = service.call({'operation': 'e3d_list_sessions', 'arguments': {}})
    if not listing['ok']:
        raise BridgeError(listing['error']['code'], listing['error']['message'], listing['error']['details'])
    modules = ['Design', 'Draw'] if args.get('module') == 'Both' else [args.get('module', 'Design')]
    available = [row for row in listing['data']['sessions'] if row.get('available')]
    selected_project=args.get('project')
    if selected_project:
        from .target import project_name
        project_name(selected_project)
        available=[row for row in available if row.get('context',{}).get('project',{}).get('name')==selected_project]
    if args.get('session_id') and not any(row['session']['session_id'] == args['session_id'] and row['session']['module'] in modules for row in available):
        return {'status': 'NEEDS_SESSION_SELECTION', 'ready': False, 'sessions': available,
                'next_action': 'Requested exact session is unavailable or belongs to another module.'}
    if not selected_project and args.get('session_id'):
        selected_project=next(row['context']['project']['name'] for row in available if row['session']['session_id']==args['session_id'])
        available=[row for row in available if row['context']['project']['name']==selected_project]
    if available and not selected_project:
        return {'status':'NEEDS_PROJECT_SELECTION','ready':False,'sessions':available,
                'next_action':'Show open project/MDB sessions and ask the user which project to use, then verify with project or an exact chosen session_id.'}
    chosen = []
    for module in modules:
        matches = [row for row in available if row['session']['module'] == module]
        explicit = args.get('session_id')
        if explicit and any(row['session']['session_id'] == explicit for row in matches):
            matches = [row for row in matches if row['session']['session_id'] == explicit]
        elif explicit and len(modules) == 1:
            return {'status': 'NEEDS_SESSION_SELECTION', 'ready': False, 'module': module,
                    'sessions': matches, 'next_action': 'Requested exact session is unavailable or belongs to another module.'}
        if len(matches) != 1:
            return {'status': 'NEEDS_PROJECT_LOGIN' if not matches else 'NEEDS_SESSION_SELECTION',
                    'ready': False, 'module': module, 'sessions': matches,
                    'next_action': 'Log into the user-selected project normally, or select an exact session_id if multiple windows match.'}
        chosen.append(matches[0])
    results = []
    for row in chosen:
        sid = row['session']['session_id']
        # check.py starts the exact packaged MCP manifest command and validates
        # fresh context, native engine, core/strict lint, receipt, and unchanged REF.
        try:
            process = subprocess.run([sys.executable, '-B', str(scripts() / 'check.py'),
                                      '--session-id', sid, '--project', selected_project, '--run-read-only'],
                                     capture_output=True, encoding='utf-8', timeout=240)
        except subprocess.TimeoutExpired as exc:
            raise BridgeError('INDETERMINATE', 'Verification timed out. Inspect existing diagnostics/runs; do not retry the probe automatically.') from exc
        try:
            result = json.loads(process.stdout)
        except ValueError as exc:
            raise BridgeError('SETUP_VERIFY_FAILED', 'Verification did not return JSON.', {'stderr': process.stderr[-4000:]}) from exc
        if process.returncode or result.get('status') != 'OK':
            code = 'INDETERMINATE' if 'INDETERMINATE' in result.get('error', '') else 'SETUP_VERIFY_FAILED'
            raise BridgeError(code, 'Initial setup live verification failed. Inspect evidence before another attempt.', result)
        if row['session'].get('engine') != 'e3d-native':
            raise BridgeError('SETUP_VERIFY_FAILED', 'Independent native bridge required.')
        results.append({'session_id': sid, 'module': row['session']['module'],
                        'evidence': result['evidence'],
                        'modeling_ready': not result['after']['currentElement']['readOnly'],
                        'context': result['after']})
    writable = all(item['modeling_ready'] for item in results)
    return {'status': 'READY' if writable else 'READY_READ_ONLY', 'ready': True,
            'modeling_ready': writable, 'sessions': results, 'savework_executed': False,
            'next_action': 'Initial setup verified. Use exact session/context guards for requested work.' if writable
            else 'Connection verified, but current element is read-only; select an authorized writable target before modeling.'}


def setup(args, service):
    if args['action'] != 'inspect' and args.get('confirm') is not True:
        raise BridgeError('CONFIRM_REQUIRED', 'apply/verify require confirm=true and an explicit initial-setup request.')
    if args['action'] == 'verify':
        result = verify(args, service)
    else:
        result = installation(args)
        if args['action'] == 'apply' and result['status'] == 'INSTALLED':
            result = {'installation': result, **verify(args, service)}
    directory = Path(os.environ.get('E3D_BRIDGE_DATA_DIR', str(Path(os.environ['LOCALAPPDATA']) / 'E3DBridge'))) / 'setup'
    directory.mkdir(parents=True, exist_ok=True)
    evidence = directory / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
    result['setup_evidence'] = str(evidence)
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
