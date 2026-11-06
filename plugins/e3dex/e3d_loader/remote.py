"""Server-side core calls. This PC sends job files and receives generated macros; it has no core code.

Paths: the server works in placeholder folders (tokens). Generated files are saved under
this PC's data folder (or the output folder the user named) and every token is turned
back into the real local path, so results look exactly as if generated locally.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import quote
import uuid

from . import LOADER_VERSION, LoaderError, data_dir, load_config, package_version
from .account import Account
from .client import Http

DATA_TOKEN = 'E3DBRIDGE_DATA_9F3A1C'
SRC_TOKEN = 'E3DBRIDGE_SRC_9F3A1C'
INPUT_ARGS = {
    'e3d_pipe_harness': ('job_file',), 'e3d_structures_harness': ('job_file',), 'e3d_equipment_harness': ('job_file',),
    'e3d_plan_profile': ('job_file', 'current_profile_file'),
    'e3d_plan_export': ('native_file', 'profile_file', 'receipt_file'),
}
OUTPUT_ARGS = {'e3d_plan_export': ('output_file',)}
MAX_UPLOAD = 80 * 1024 * 1024
MAX_RESPONSE = 400 * 1024 * 1024
PROFILE_NAME = re.compile(r'[A-Za-z0-9가-힣_-]{1,64}')


def ext_token(index):
    return 'E3DBRIDGE_EXT%d_9F3A1C' % index


def _post(path, body, timeout):
    config = load_config()
    account = Account(config, data_dir(), Http(config['server_url']))
    http = account.http
    token = account.access_token()
    headers = {'X-E3D-App-Version': package_version(), 'X-E3D-Loader-Version': LOADER_VERSION}
    for attempt in range(2):
        status, payload = http.request('POST', path, json_body=body, token=token, headers=headers, timeout=timeout,
                                       max_response=MAX_RESPONSE)
        if status == 200:
            return payload
        if status == 401 and attempt == 0:
            token = account.access_token(force_refresh=True)
            continue
        if status == 402:
            account._fail('PAYMENT_REQUIRED')
        if status == 403:
            account._fail('ACCESS_REVOKED' if payload.get('error') == 'access_revoked' else 'NOT_APPROVED',
                          payload.get('error_description'))
        if status == 401:
            account._fail('LOGIN_REQUIRED')
        raise LoaderError('SERVER_ERROR', '서버 처리 실패 (HTTP %d): %s' % (status, payload.get('error_description', '')))
    account._fail('LOGIN_REQUIRED')


def _b64(path):
    return base64.b64encode(Path(path).read_bytes()).decode('ascii')


def _safe_parts(rel):
    parts = rel.split('/')
    if len(parts) < 2 or any(p in ('', '.', '..') or ':' in p or '\\' in p or '\x00' in p for p in parts):
        raise LoaderError('SERVER_ERROR', 'Server returned an unsafe file name')
    return parts


class _Localizer:
    """Turns server tokens into this PC's real folders."""

    def __init__(self, folders):
        self.folders = folders  # token -> Path
        self.sha = {}

    def text(self, value):
        for token, folder in self.folders.items():
            value = value.replace(token, folder.as_posix())
        return value

    def value(self, value):
        if isinstance(value, str):
            if value in self.sha:
                return self.sha[value]
            for token, folder in self.folders.items():
                if value == token or value.startswith(token + '/'):
                    return str(folder.joinpath(*[p for p in value[len(token):].split('/') if p]))
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        return value


def core_call(request):
    """Run a core operation on the server. Returns the same envelope as a local run."""
    from e3d_bridge.errors import BridgeError
    from e3d_bridge.service import envelope
    try:
        operation = request['operation']
        arguments = dict(request['arguments'])
        local_data = data_dir()
        folders = {DATA_TOKEN: local_data}
        writable = {DATA_TOKEN}
        files, size, index = {}, 0, 0
        for key in INPUT_ARGS[operation]:
            if key not in arguments:
                continue
            path = Path(arguments[key])
            if not path.is_absolute() or not path.is_file():
                raise LoaderError('INVALID_REQUEST', '%s must be an existing absolute file path' % key)
            size += path.stat().st_size
            if size > MAX_UPLOAD:
                raise LoaderError('INVALID_REQUEST', 'Input files are too large (limit 80 MB)')
            token = ext_token(index)
            index += 1
            files['%s/%s' % (token, path.name)] = _b64(path)
            arguments[key] = '%s/%s' % (token, path.name)
            folders[token] = path.parent
        for key in OUTPUT_ARGS.get(operation, ()):
            if key not in arguments:
                continue
            path = Path(arguments[key])
            if not path.is_absolute():
                raise LoaderError('INVALID_REQUEST', '%s must be an absolute path' % key)
            token = ext_token(index)
            index += 1
            arguments[key] = '%s/%s' % (token, path.name)
            folders[token] = path.parent
            writable.add(token)
        profile = arguments.get('user_profile')
        if operation == 'e3d_plan_profile' and isinstance(profile, str) and PROFILE_NAME.fullmatch(profile):
            stored = local_data / 'plan' / 'users' / (profile + '.json')
            if stored.is_file():
                files['%s/plan/users/%s.json' % (DATA_TOKEN, profile)] = _b64(stored)
        payload = _post('/api/v1/core/call', {'operation': operation, 'arguments': arguments, 'files': files,
                                              'ext_dirs': index, 'request_id': request.get('request_id')}, timeout=200)
        localizer = _Localizer(folders)
        written = []
        for rel, item in sorted((payload.get('files') or {}).items()):
            parts = _safe_parts(rel)
            if parts[0] not in writable:
                continue
            data = base64.b64decode(item['data'])
            try:
                text = data.decode('utf-8')
                data = localizer.text(text).encode('utf-8')
            except UnicodeDecodeError:
                pass
            target = folders[parts[0]].joinpath(*parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
            tmp.write_bytes(data)
            os.replace(tmp, target)
            new = hashlib.sha256(data).hexdigest()
            if new != item.get('sha256'):
                localizer.sha[item['sha256']] = new
            written.append(target)
        if localizer.sha:  # keep manifest hashes consistent with the localized macros
            for target in written:
                if target.suffix == '.json':
                    text = target.read_text(encoding='utf-8')
                    for old, new in localizer.sha.items():
                        text = text.replace(old, new)
                    target.write_text(text, encoding='utf-8')
        return localizer.value(payload['envelope'])
    except LoaderError as exc:
        return envelope(request, error=BridgeError(exc.code, str(exc), exc.details))


def remote_lint(sources, run_dir, effect):
    """Same contract as the harness's local lint(): writes lint reports into run_dir."""
    profiles = ['core', 'strict'] + (['safety'] if effect == 'model' else [])
    files = {'%s/%s' % (SRC_TOKEN, Path(s).name): _b64(s) for s in sources}
    payload = _post('/api/v1/core/lint', {'profiles': profiles, 'files': files}, timeout=120)
    localizer = _Localizer({SRC_TOKEN: Path(run_dir)})
    results = []
    for item in payload['lint']:
        profile = item['profile']
        (Path(run_dir) / ('lint-%s.stdout.txt' % profile)).write_bytes(localizer.text(item.get('stdout') or '').encode('utf-8'))
        (Path(run_dir) / ('lint-%s.stderr.txt' % profile)).write_bytes(localizer.text(item.get('stderr') or '').encode('utf-8'))
        if item.get('report') is not None:
            (Path(run_dir) / ('lint-%s.json' % profile)).write_text(localizer.text(item['report']), encoding='utf-8')
        results.append({'profile': profile, 'exit_code': int(item['exit_code'])})
    return results


def guide(topic=None):
    config = load_config()
    account = Account(config, data_dir(), Http(config['server_url']))
    path = '/api/v1/guide' + ('?topic=' + quote(topic, safe='') if topic else '')
    token = account.access_token()
    for attempt in range(2):
        status, payload = account.http.request('GET', path, token=token, timeout=20, max_response=8 * 1024 * 1024)
        if status == 200:
            return payload
        if status == 401 and attempt == 0:
            token = account.access_token(force_refresh=True)
            continue
        if status == 402:
            account._fail('PAYMENT_REQUIRED')
        if status == 403:
            account._fail('ACCESS_REVOKED' if payload.get('error') == 'access_revoked' else 'NOT_APPROVED',
                          payload.get('error_description'))
        if status == 400:
            raise LoaderError('INVALID_REQUEST', payload.get('error_description', 'Unknown topic'))
        raise LoaderError('SERVER_ERROR', '가이드를 가져오지 못했습니다 (HTTP %d).' % status)
    account._fail('LOGIN_REQUIRED')
