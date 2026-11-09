"""Execute reviewed PML macros in a local E3D session and retain runtime evidence."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parent
BUNDLED_LINTER = ROOT.parents[1] / 'tools/pml_lint.py'
DEFAULT_LINTER = BUNDLED_LINTER if BUNDLED_LINTER.is_file() else Path.home() / '.codex/skills/e3d-pml-development/scripts/pml_lint.py'
DEFAULT_RUNS = Path(os.environ['E3D_BRIDGE_DATA_DIR']) / 'runs' if os.environ.get('E3D_BRIDGE_DATA_DIR') else ROOT / 'runs'
EXIT_CODES = {'OK': 0, 'PML_ERROR': 2, 'CHECK_FAILED': 3, 'NOT_RUN': 4, 'INDETERMINATE': 5}


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def scope(context):
    return {
        'project': context['project']['name'],
        'mdb': context['mdb']['name'],
        'ref': context['currentElement']['ref'],
    }


class Bridge:
    def __init__(self, url='http://127.0.0.1:18790', timeout=20, session_id=None, expected=None, effect=None):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('Use a local HTTP bridge origin, e.g. http://127.0.0.1:18790')
        if timeout <= 0:
            raise ValueError('Timeout must be positive')
        self.url = url.rstrip('/')
        self.timeout = timeout
        self.session_id = session_id
        self.expected = expected
        self.effect = effect
        # Local bridge traffic must not be routed through a configured proxy.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def verify_session(self):
        if self.session_id is None:
            return
        sys.path.insert(0, str(ROOT.parents[1]))
        from e3d_bridge.service import load_backend
        from e3d_bridge.errors import BridgeError
        checked = load_backend().session_context(self.session_id)
        if checked['session']['http_url'] != self.url:
            raise BridgeError('SESSION_MISMATCH', 'Session endpoint changed before command submission')
        if self.expected and scope(checked['context']) != self.expected:
            raise BridgeError('TARGET_CHANGED', 'Target changed before command submission')
        if self.effect == 'model' and checked['context']['currentElement'].get('readOnly') is not False:
            raise BridgeError('READ_ONLY', 'Current element is not writable')

    def request(self, path, payload=None):
        if path == '/command':
            self.verify_session()
            if self.session_id and self.expected and payload:
                payload = dict(payload, session_id=self.session_id, effect=self.effect, **self.expected)
                command = payload.get('command', '')
                if command.startswith('$M "') and command.endswith('"'):
                    payload['macro_sha256'] = hashlib.sha256(Path(command[4:-1]).read_bytes()).hexdigest()
        body = None if payload is None else json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(self.url + path, data=body,
                                         headers={'Content-Type': 'application/json; charset=utf-8'})
        # Intentionally exactly one attempt: a timed-out command may have executed.
        with self.opener.open(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode('utf-8-sig'))

    def context(self):
        result = self.request('/context')
        if result.get('ok') is not True:
            raise RuntimeError('Bridge context failed: ' + str(result))
        context = result['data']
        if not context['project'].get('loggedIn') or not context['currentElement'].get('isValid'):
            raise RuntimeError('E3D must be logged in with a valid current element')
        scope(context)  # Validate required fields before sending any PML.
        return context


def pml_literal(value):
    # Reject macro interpolation and string delimiters, rather than guessing escaping.
    if any(c in value for c in "'\"$\r\n\x00"):
        raise ValueError('PML path/value contains an unsupported quote, dollar sign or control character')
    value.encode('cp949')
    return "'" + value + "'"


def macro_command(path):
    value = path.resolve().as_posix()
    pml_literal(value)
    return '$M "' + value + '"'


def build_wrapper(source, receipt, run_id, expected_ref):
    pml_literal(run_id)
    ref = pml_literal(expected_ref)
    output = pml_literal(receipt.resolve().as_posix())
    return '\n'.join([
        '-- Generated by E3D Harness. Reserved variables: !!E3DHarnessData and !eh*.',
        '!!E3DHarnessData = object ARRAY()',
        '!ehSourceDir = ' + pml_literal(source.parent.resolve().as_posix()),
        '!!E3DHarnessSourceDir = !ehSourceDir',
        '!ehRows = object ARRAY()',
        f"!ehRows.append('RUN_ID={run_id}')",
        "!ehStatus = 'OK'",
        'var !ehStartRef REFNO',
        "!ehRows.append('START_REF=' & !ehStartRef)",
        f'if (!ehStartRef neq {ref}) then',
        "  !ehStatus = 'SCOPE_MISMATCH'",
        "  !ehRows.append('ERROR=Current element changed before the macro started.')",
        'else',
        '  ' + macro_command(source),
        '  handle any',
        '    !ehErrorText = !!error.text',
        '    !ehErrorLine = !!error.line',
        '    !ehErrorCommand = !!error.command',
        '    !ehErrorStack = !!error.callstack',
        "    !ehStatus = 'ERROR'",
        "    !ehRows.append('ERROR=' & !ehErrorText)",
        "    !ehRows.append('ERROR_LINE=' & !ehErrorLine)",
        "    !ehRows.append('ERROR_COMMAND=' & !ehErrorCommand)",
        '    do !ehStackItem values !ehErrorStack',
        "      !ehRows.append('STACK=' & !ehStackItem)",
        '    enddo',
        '  endhandle',
        'endif',
        'var !ehEndRef REFNO',
        "!ehRows.append('END_REF=' & !ehEndRef)",
        'do !ehItem values !!E3DHarnessData',
        "  !ehRows.append('DATA=' & !ehItem)",
        'enddo',
        "!ehRows.append('STATUS=' & !ehStatus)",
        f'!ehFile = object FILE({output})',
        "!ehFile.writeFile('OVERWRITE', !ehRows)",
        '!ehFile.close()',
        '',
    ])


def read_receipt(path, run_id):
    raw = path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('Receipt exceeds 4 MiB')
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        content = raw.decode('cp949')
    records = {}
    for line in content.splitlines():
        key, separator, value = line.partition('=')
        if not separator or key not in {
            'RUN_ID', 'STATUS', 'START_REF', 'END_REF', 'ERROR', 'ERROR_LINE',
            'ERROR_COMMAND', 'STACK', 'DATA',
        }:
            raise ValueError('Malformed receipt line: ' + line[:160])
        records.setdefault(key, []).append(value)
    for key in ('RUN_ID', 'STATUS', 'START_REF', 'END_REF'):
        if len(records.get(key, [])) != 1:
            raise ValueError('Missing or duplicate receipt field: ' + key)
    if records['RUN_ID'] != [run_id]:
        raise ValueError('Receipt belongs to a different run')
    if records['STATUS'][0] not in ('OK', 'ERROR', 'SCOPE_MISMATCH'):
        raise ValueError('Unknown PML status')
    return records


def assess(bridge_result, receipt, before, after, effect, expectations, transport_error=None):
    if transport_error:
        return 'INDETERMINATE', ['Command transport failed; do not replay automatically.']
    if receipt is None:
        return 'INDETERMINATE', ['No valid completion receipt; execution is unconfirmed.']
    status = receipt['STATUS'][0]
    if status == 'ERROR':
        return 'PML_ERROR', receipt.get('ERROR', ['PML failed.'])
    if status == 'SCOPE_MISMATCH':
        return 'NOT_RUN', ['Current element changed before execution; macro was not started.']
    if not isinstance(bridge_result, dict) or bridge_result.get('ok') is not True:
        return 'INDETERMINATE', ['PML receipt and bridge response disagree.']
    issues = []
    if receipt['START_REF'][0] != scope(before)['ref']:
        issues.append('Starting element does not match the recorded context.')
    if after is None:
        issues.append('Could not read E3D context after execution.')
    else:
        original, current = scope(before), scope(after)
        for key in ('project', 'mdb'):
            if original[key] != current[key]:
                issues.append(key + ' changed during execution.')
        if effect == 'read-only' and (
            original['ref'] != current['ref'] or receipt['END_REF'][0] != original['ref']
        ):
            issues.append('Current element changed during a read-only run.')
    data = receipt.get('DATA', [])
    for expected in expectations:
        if expected not in data:
            issues.append('Missing expected data: ' + expected)
    return ('CHECK_FAILED', issues) if issues else ('OK', [])


@contextmanager
def run_lock(root, run_id):
    lock = root / '.harness.lock'
    with lock.open('x', encoding='ascii') as handle:
        handle.write(json.dumps({'run_id': run_id, 'pid': os.getpid()}))
    try:
        yield
    finally:
        # Remove only this runner's lock; never sweep other files.
        if lock.exists() and json.loads(lock.read_text(encoding='ascii')).get('run_id') == run_id:
            lock.unlink()


def lint(sources, run_dir, linter, effect):
    if not BUNDLED_LINTER.is_file():  # public package: lint runs on the approval server
        sys.path.insert(0, str(ROOT.parents[1]))
        from e3d_loader.remote import remote_lint
        return remote_lint(sources, run_dir, effect)
    if not linter.is_file():
        raise FileNotFoundError('PML linter not found; set --linter: ' + str(linter))
    results = []
    for profile in ('core', 'strict') + (('safety',) if effect == 'model' else ()):
        command = [sys.executable, str(linter), *map(str, sources), '--profile', profile,
                   '--format', 'json', '--fail-on', 'error',
                   '--report-json', str(run_dir / f'lint-{profile}.json')]
        process = subprocess.run(command, capture_output=True, timeout=30)
        (run_dir / f'lint-{profile}.stdout.txt').write_bytes(process.stdout)
        (run_dir / f'lint-{profile}.stderr.txt').write_bytes(process.stderr)
        results.append({'profile': profile, 'exit_code': process.returncode})
    return results


def deploy_library(library, snapshots, run_dir, report):
    """Deploy only this project's eh* definitions into a dedicated local subfolder."""
    library = library.resolve(strict=True)
    if str(library).startswith(('\\\\', '//')):
        raise ValueError('Development deployment requires a local PMLLIB directory')
    scanner = library.parent / 'pmlscan.exe'
    if library.name.lower() != 'pmllib' or not scanner.is_file():
        raise ValueError('Expected an installed local PMLLIB with ../pmlscan.exe')
    definitions = [p for p in snapshots if p.suffix in ('.pmlfnc', '.pmlfrm', '.pmlobj')]
    if not definitions or any(not p.name.startswith('eh') for p in definitions):
        raise ValueError('Development deployment accepts included eh* definitions only')
    destination = library / 'e3d_harness_dev'
    if destination.exists() and destination.resolve().parent != library:
        raise ValueError('Development destination resolves outside the library')
    backup = run_dir / 'deployment-backup'
    backup.mkdir()
    index = library / 'pml.index'
    if index.exists():
        shutil.copy2(index, backup / 'pml.index')
    destination.mkdir(exist_ok=True)
    report['deployment'] = {'directory': str(destination), 'files': [], 'scan_complete': False}
    save_json(run_dir / 'deployment.json', report['deployment'])
    for definition in definitions:
        target = destination / definition.name
        if target.exists() and (target.is_symlink() or target.resolve().parent != destination.resolve()):
            raise ValueError('Runtime definition resolves outside the development folder')
        previous_hash = None
        if target.exists():
            previous_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            shutil.copy2(target, backup / target.name)
        shutil.copy2(definition, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != hashlib.sha256(definition.read_bytes()).hexdigest():
            raise RuntimeError('Deployed source hash mismatch: ' + str(target))
        report['deployment']['files'].append({'path': str(target), 'sha256': digest,
                                               'previous_sha256': previous_hash})
        save_json(run_dir / 'deployment.json', report['deployment'])
    result = subprocess.run([str(scanner), str(library)], capture_output=True, timeout=45)
    (run_dir / 'pmlscan.stdout.txt').write_bytes(result.stdout)
    (run_dir / 'pmlscan.stderr.txt').write_bytes(result.stderr)
    report['deployment']['scanner_exit_code'] = result.returncode
    report['deployment']['scan_complete'] = result.returncode == 0
    save_json(run_dir / 'deployment.json', report['deployment'])
    if result.returncode != 0:
        raise RuntimeError('pmlscan failed; see deployment logs. E3D entry macro was not sent.')


def verify_deployment(deployment, receipt):
    issues = []
    data = (receipt or {}).get('DATA', [])
    for entry in deployment['files']:
        target = Path(entry['path'])
        if hashlib.sha256(target.read_bytes()).hexdigest() != entry['sha256']:
            issues.append('Runtime source changed during execution: ' + str(target))
        prefix = 'loaded_path.' + target.name + '='
        matches = [row[len(prefix):] for row in data if row.startswith(prefix)]
        if len(matches) != 1 or Path(matches[0]).resolve() != target.resolve():
            issues.append('Runtime loading path was not verified: ' + target.name)
    return issues


def write_report(run_dir, report):
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    save_json(run_dir / 'report.json', report)
    receipt = report.get('receipt') or {}
    lines = [f"# PML run: {report['status']}", '', f"- Run: `{report['run_id']}`",
             f"- Source: `{report['source']}`", f"- SHA-256: `{report['sha256']}`",
             f"- Declared effect: `{report['effect']}`",
             f"- Previous run: `{report.get('previous') or '-'}`", '',
             '## Findings', '']
    lines += ['- ' + issue for issue in report.get('issues', [])] or ['Macro completed and configured checks passed.']
    for key in ('ERROR', 'ERROR_LINE', 'ERROR_COMMAND', 'STACK', 'DATA'):
        if receipt.get(key):
            lines += ['', '## ' + key, '', '```text', *receipt[key], '```']
    lines += ['', 'Completion is not a geometry, engineering, or UI acceptance test.',
              'Read-only/session/model is a reviewed intent label, not a PML sandbox.', '']
    (run_dir / 'report.md').write_text('\n'.join(lines), encoding='utf-8')


def execute(args, bridge=None):
    source = args.source.resolve(strict=True)
    if source.suffix.lower() != '.pmlmac':
        raise ValueError('run accepts .pmlmac entry macros; use a driver for forms/functions/objects')
    if source.name.lower() == 'wrapper.pmlmac':
        raise ValueError('wrapper.pmlmac is a reserved entry filename')
    if args.effect == 'model' and not all((args.project, args.mdb, args.ref)):
        raise ValueError('Model runs require explicit --project, --mdb and --ref scope')
    if args.deploy_library and args.effect == 'read-only':
        raise ValueError('Library deployment requires --effect session or model')
    if source.read_text(encoding='utf-8-sig').startswith('-- E3D_WORK_TARGET='):
        sys.path.insert(0, str(ROOT.parents[1]))
        from e3d_bridge.target import check_macro_target
        check_macro_target(source,dict(session_id=getattr(args,'session_id',None),project=args.project,mdb=args.mdb))
    bridge = bridge or Bridge(args.url, args.timeout, getattr(args, 'session_id', None),
                              {'project': args.project, 'mdb': args.mdb, 'ref': args.ref}, args.effect)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:12]
    runs_root = args.runs.resolve()
    runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = runs_root / run_id
    run_dir.mkdir()
    raw = source.read_bytes()
    snapshot = run_dir / source.name
    snapshot.write_bytes(raw)
    snapshots = [snapshot]
    artifacts = []
    names = {source.name.lower(), 'wrapper.pmlmac', 'pml-result.txt', 'report.json', 'report.md'}
    for dependency in args.include:
        dependency = dependency.resolve(strict=True)
        if dependency.suffix.lower() not in ('.pmlmac', '.pmlfnc', '.pmlobj', '.pmlfrm', '.pmldat'):
            raise ValueError('Unsupported PML dependency: ' + str(dependency))
        if dependency.name.lower() in names:
            raise ValueError('Duplicate/reserved artifact filename: ' + dependency.name)
        names.add(dependency.name.lower())
        content = dependency.read_bytes()
        staged = run_dir / dependency.name
        staged.write_bytes(content)
        snapshots.append(staged)
        artifacts.append({'source': str(dependency), 'snapshot': str(staged),
                          'sha256': hashlib.sha256(content).hexdigest()})
    report = {'run_id': run_id, 'source': str(source), 'snapshot': str(snapshot),
              'sha256': hashlib.sha256(raw).hexdigest(), 'effect': args.effect,
              'previous': str(args.previous.resolve()) if args.previous else None,
              'expectations': args.expect, 'status': 'NOT_RUN', 'issues': [],
              'artifacts': artifacts,
              'command_sent': False, 'receipt': None}
    receipt_path = run_dir / 'pml-result.txt'
    before = after = response = None
    save_json(run_dir / 'report.json', report)
    try:
        with run_lock(runs_root, run_id):
            report['lint'] = lint(snapshots, run_dir, args.linter, args.effect)
            if any(result['exit_code'] != 0 for result in report['lint']):
                raise RuntimeError('Lint failed; inspect lint reports before execution.')
            before = bridge.context()
            save_json(run_dir / 'context-before.json', before)
            for key, expected in (('project', args.project), ('mdb', args.mdb), ('ref', args.ref)):
                if expected is not None and scope(before)[key] != expected:
                    raise RuntimeError(f'Scope mismatch: {key} expected {expected}, got {scope(before)[key]}')
            if args.deploy_library:
                deploy_library(args.deploy_library, snapshots, run_dir, report)
            wrapper = run_dir / 'wrapper.pmlmac'
            wrapper.write_bytes(build_wrapper(snapshot, receipt_path, run_id, scope(before)['ref']).encode('cp949'))
            command = macro_command(wrapper)
            save_json(run_dir / 'request.json', {'command': command})
            # Recheck project/MDB/element immediately before submitting the wrapper.
            preflight = bridge.context()
            save_json(run_dir / 'context-preflight.json', preflight)
            if scope(before) != scope(preflight):
                raise RuntimeError('E3D scope changed during preparation; macro was not sent.')
            transport_error = None
            report['command_sent'] = True
            report['status'] = 'INDETERMINATE'
            save_json(run_dir / 'report.json', report)
            try:
                response = bridge.request('/command', {'command': command})
                save_json(run_dir / 'bridge-response.json', response)
            except Exception as error:
                transport_error = type(error).__name__ + ': ' + str(error)
                report['transport_error'] = transport_error
            try:
                report['receipt'] = read_receipt(receipt_path, run_id)
            except (OSError, ValueError, UnicodeError) as error:
                report['receipt_error'] = str(error)
            try:
                after = bridge.context()
                save_json(run_dir / 'context-after.json', after)
            except Exception as error:
                report['context_after_error'] = str(error)
            report['status'], report['issues'] = assess(
                response, report['receipt'], before, after, args.effect, args.expect, transport_error)
            if report.get('deployment'):
                report['deployment_checks'] = verify_deployment(report['deployment'], report['receipt'])
                if report['status'] == 'OK' and report['deployment_checks']:
                    report['status'] = 'CHECK_FAILED'
                    report['issues'].extend(report['deployment_checks'])
    except Exception as error:
        report['status'] = 'INDETERMINATE' if report['command_sent'] else 'NOT_RUN'
        report['issues'].append(type(error).__name__ + ': ' + str(error))
    write_report(run_dir, report)
    return report, run_dir


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:18790')
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--session-id', help='Revalidate exact selected E3D process and target immediately before command submission')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('status', help='Read bridge health and current E3D context')
    run = commands.add_parser('run', help='Lint, snapshot, execute, capture errors and check results')
    run.add_argument('source', type=Path)
    run.add_argument('--effect', choices=('read-only', 'session', 'model'), required=True)
    run.add_argument('--project')
    run.add_argument('--mdb')
    run.add_argument('--ref')
    run.add_argument('--expect', action='append', default=[], metavar='KEY=VALUE')
    run.add_argument('--include', type=Path, action='append', default=[],
                     help='Snapshot/lint a PML dependency; access via !!E3DHarnessSourceDir')
    run.add_argument('--deploy-library', type=Path,
                     help='Deploy included eh* definitions to a local PMLLIB/e3d_harness_dev and scan its index')
    run.add_argument('--previous', type=Path, help='Previous run directory when repairing/revalidating')
    run.add_argument('--runs', type=Path, default=DEFAULT_RUNS)
    run.add_argument('--linter', type=Path, default=DEFAULT_LINTER)
    show = commands.add_parser('report', help='Read a previous run without sending commands to E3D')
    show.add_argument('directory', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == 'report':
            print((args.directory / 'report.md').read_text(encoding='utf-8'))
            return 0
        if args.action == 'status':
            bridge = Bridge(args.url, args.timeout)
            print(json.dumps({'health': bridge.request('/health'), 'context': bridge.context()},
                             ensure_ascii=False, indent=2))
            return 0
        report, directory = execute(args)
        print(json.dumps({'status': report['status'], 'issues': report['issues'],
                          'data': (report.get('receipt') or {}).get('DATA', []),
                          'report': str(directory / 'report.md')}, ensure_ascii=False, indent=2))
        return EXIT_CODES[report['status']]
    except (OSError, ValueError, RuntimeError) as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        return 4


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
