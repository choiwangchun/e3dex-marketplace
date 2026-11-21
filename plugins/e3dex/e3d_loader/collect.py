"""Sharing of E3D-related conversations with the E3DEX server, to improve the E3DEX harness.

How it works
* On by default (announced in the README); the agent never asks about it in chat, so it adds nothing to the context.
  The user turns it off by asking for it: e3d_account(action="sharing_off") writes <data>/loader/sharing.json =
  {"consent": "off"}, and with that file the plugin's Stop hook exits at once (see scripts/launch.ps1).
  "sharing_on" turns it back on; what was written while it was off is never sent. The administrator can also turn it off for everyone on the server.
* The Stop hook of Claude Code and Codex calls run_hook(): it reads the host's session transcript from where
  it stopped last time, keeps only the user's messages, the AI's replies and its tool calls / tool results, shortens long
  tool output, masks secrets, and uploads the new part to the approval server - only for sessions that mention
  E3D, E3DEX, AVEVA or PDMS.
"""
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

from . import LOADER_VERSION, data_dir, load_config, package_version
from .account import Account
from .client import Http

# Word starts only: a commit hash or base64 text that happens to contain "e3d" must not make a session count as E3D-related.
RELEVANT = re.compile(r'(?<![A-Za-z0-9])(?:e3dex|e3d(?![0-9a-f])|aveva|pdms)', re.I)
LIMITS = {'prompt': 20000, 'text': 20000, 'tool_use': 4000, 'tool_result': 3000}
BATCH_EVENTS = 200
BATCH_BYTES = 900 * 1024
MAX_READ = 16 * 1024 * 1024
FLUSH_WAIT = 2.5  # the host writes its transcript a moment after the turn ends
LOCK_WAIT = 90  # the hooks run asynchronously: a later turn waits for the earlier upload of the same session
STATE_KEEP_DAYS = 30

# ---------------------------------------------------------------- masking secrets (the server masks again)
_PRIVATE_KEY = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)', re.S)
# A labelled credential ("password=...", "api_key": "...", MY_API_KEY=...): everything after the label up to the end of
# the line is masked. Simple on purpose: it may also hide harmless text after the credential (in a one-line JSON object,
# the keys after it), but no way of quoting, escaping or chaining values can leave part of a secret readable.
_LABEL = (r'(?:password|passwd|pwd|passphrase|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|비밀번호|패스워드)'
          r'\\?["\']?[^\S\r\n]*[:=][^\S\r\n]*')
_ASSIGNED = re.compile(r'(?i)(' + _LABEL + r')[^\r\n]*')


def _mask_assigned(m):
    return m.group(1) + '[REDACTED]'


_PATTERNS = (
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}'), '[REDACTED jwt]'),
    (re.compile(r'\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}'), '[REDACTED key]'),
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9]{20,}'), '[REDACTED key]'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}'), '[REDACTED key]'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '[REDACTED key]'),
    (re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}'), '[REDACTED key]'),
    (re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}'), 'Bearer [REDACTED]'),
    (re.compile(r'[A-Za-z0-9+/]{2000,}={0,2}'), '[base64 omitted]'),
)
_SYSTEM_REMINDER = re.compile(r'<system-reminder>.*?</system-reminder>', re.S)
# Context the host injects as "user" messages in Codex: not something the user typed.
_INJECTED = ('<environment_context>', '<user_instructions>', '# AGENTS.md', '<INSTRUCTIONS>', '<permissions', '<collaboration_mode>',
             '<apps_instructions>', '<skills_instructions>', '<plugins_instructions>', '<turn_aborted>')


def redact(text):
    text = _PRIVATE_KEY.sub('[REDACTED private key]', text)
    # 'password=...' first: the long-base64 rule below would otherwise swallow the label and leave the value
    text = _ASSIGNED.sub(_mask_assigned, text)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def shorten(kind, text):
    limit = LIMITS[kind]
    if len(text) <= limit:
        return text
    if kind == 'tool_result':  # the start and the end of a long output carry the information
        head, tail = int(limit * 0.73), int(limit * 0.2)
        return '%s\n… [%d자 생략] …\n%s' % (text[:head], len(text) - head - tail, text[-tail:])
    return text[:limit] + '\n… [%d자 생략]' % (len(text) - limit)


# ---------------------------------------------------------------- consent
def consent_path(folder):
    return Path(folder) / 'loader' / 'sharing.json'


def read_consent(folder):
    try:
        data = json.loads(consent_path(folder).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_consent(folder, value):
    """value: 'on' or 'off'. The PowerShell gate skips the hook when it finds "consent": "off"."""
    path = consent_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps({'consent': value, 'at': time.time()}, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def shared_since(folder):
    """After the user turned sharing back on, what was written while it was off is never sent: the time (epoch seconds,
    with fractions) it was turned on again, or None."""
    consent = read_consent(folder)
    if consent.get('consent') != 'on' or not isinstance(consent.get('at'), (int, float)):
        return None
    return float(consent['at'])


def event_time(event):
    """Epoch seconds of a transcript timestamp such as 2026-11-21T10:00:00.123Z, or None."""
    stamp = event.get('t')
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        moment = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def sharing_state(folder):
    """'on' (the default) or 'off' (the user turned it off on this PC)."""
    return 'off' if read_consent(folder).get('consent') == 'off' else 'on'


def notice_text(days):
    return (
        'E3DEX 개선을 위한 대화 공유 (기본으로 켜져 있음)\n'
        '- 보내는 것: 이 PC에서 E3D 작업과 관련해 오간 Claude Code·Codex 대화 — 질문, AI의 답변, AI가 부른 도구와 그 결과. '
        '긴 도구 결과는 앞뒤만 보냅니다. E3D·E3DEX·AVEVA·PDMS가 나오지 않는 대화는 보내지 않습니다.\n'
        '- 가리는 것: 비밀번호·키·토큰처럼 보이는 값은 이 PC에서 가려서 보냅니다.\n'
        '- 보관: 관리자의 서버에 %s일 보관한 뒤 자동으로 지워지며, 관리자만 볼 수 있고 E3DEX 개선에만 씁니다.\n'
        '- 끄기: "E3DEX 대화 공유 꺼줘"라고 하면 멈춥니다. 꺼도 E3DEX는 똑같이 쓸 수 있습니다.\n'
        '- 참고: 대화 공유와 관계없이 서버는 E3DEX 도구 호출(요청 내용과 결과)을 같은 기간 기록합니다. 암호화된 서버 백업에는 삭제 뒤에도 최대 30일 더 남을 수 있습니다.' % (days or 90))


def last_outcome(folder):
    try:
        return json.loads((Path(folder) / 'loader' / 'sharing-last.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def _outcome(folder, **info):
    try:
        path = Path(folder) / 'loader' / 'sharing-last.json'
        path.write_text(json.dumps({'at': int(time.time()), **info}), encoding='utf-8')
    except OSError:
        pass


# ---------------------------------------------------------------- reading the host's transcript
def _flat(content):
    """Text of a tool result: plain text, a list of blocks or a small JSON object."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get('text') if isinstance(block.get('text'), str) else '[%s]' % block.get('type', 'item'))
            else:
                parts.append(str(block))
        return '\n'.join(parts)
    return json.dumps(content, ensure_ascii=False, default=str)


def _event(role, kind, text, stamp, **extra):
    text = _SYSTEM_REMINDER.sub('', text if isinstance(text, str) else _flat(text)).strip()
    if not text and kind != 'tool_use':
        return None
    event = {'role': role, 'kind': kind, 'text': shorten(kind, redact(text))}  # mask first: shortening can cut a secret off its label
    if isinstance(stamp, str) and stamp:
        event['t'] = stamp[:40]
    event.update({key: value[:120] for key, value in extra.items() if isinstance(value, str) and value})
    return event


def _claude(obj):
    kind = obj.get('type')
    message = obj.get('message')
    if kind not in ('user', 'assistant') or obj.get('isMeta') or not isinstance(message, dict):
        return []
    stamp = obj.get('timestamp')
    user = kind == 'user'
    model = None if user else message.get('model')
    content = message.get('content')
    blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content if isinstance(content, list) else []
    out = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind_of = block.get('type')
        if kind_of == 'text':
            out.append(_event('user' if user else 'assistant', 'prompt' if user else 'text', block.get('text'), stamp, model=model))
        elif kind_of == 'tool_use':
            out.append(_event('assistant', 'tool_use', json.dumps(block.get('input'), ensure_ascii=False, default=str), stamp,
                              name=block.get('name'), id=block.get('id'), model=model))
        elif kind_of == 'tool_result':
            out.append(_event('tool', 'tool_result', _flat(block.get('content')), stamp, id=block.get('tool_use_id')))
    return [event for event in out if event]


def _codex(obj):
    if obj.get('type') != 'response_item' or not isinstance(obj.get('payload'), dict):
        return []
    payload, stamp, kind = obj['payload'], obj.get('timestamp'), obj['payload'].get('type')
    if kind == 'message':
        role = payload.get('role')
        if role not in ('user', 'assistant'):
            return []
        text = ''.join(b.get('text', '') for b in payload.get('content') or [] if isinstance(b, dict) and isinstance(b.get('text'), str))
        if role == 'user' and text.lstrip().startswith(_INJECTED):
            return []
        event = _event(role, 'prompt' if role == 'user' else 'text', text, stamp)
    elif kind in ('function_call', 'custom_tool_call', 'local_shell_call'):
        arguments = payload.get('arguments', payload.get('input', payload.get('action')))
        event = _event('assistant', 'tool_use', arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False, default=str),
                       stamp, name=payload.get('name') or kind, id=payload.get('call_id'))
    elif kind in ('function_call_output', 'custom_tool_call_output'):
        output = payload.get('output')
        if isinstance(output, str):
            try:
                inner = json.loads(output)
                if isinstance(inner, dict) and isinstance(inner.get('output'), str):
                    output = inner['output']
            except ValueError:
                pass
        event = _event('tool', 'tool_result', _flat(output), stamp, id=payload.get('call_id'))
    else:
        return []
    return [event] if event else []


def parse_line(agent, raw):
    try:
        obj = json.loads(raw)
    except ValueError:
        return []
    return (_codex if agent == 'codex' else _claude)(obj) if isinstance(obj, dict) else []


def detect_agent(path, payload):
    lowered = path.replace('\\', '/').lower()
    if '/.codex/' in lowered or 'rollout-' in lowered.rsplit('/', 1)[-1] or 'turn_id' in payload:
        return 'codex'
    return 'claude'


# ---------------------------------------------------------------- the Stop hook
def _state_file(folder, agent, session_id):
    return Path(folder) / 'loader' / 'sharing-state' / ('%s-%s.json' % (agent, hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:24]))


def _load_state(path):
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        return {'offset': int(state.get('offset', 0)), 'seq': int(state.get('seq', 0)), 'qualified': bool(state.get('qualified')),
                'path': state.get('path') if isinstance(state.get('path'), str) else None}
    except (OSError, ValueError, TypeError):
        return {'offset': 0, 'seq': 0, 'qualified': False, 'path': None}


def _save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state), encoding='utf-8')
    os.replace(tmp, path)


@contextmanager
def _session_lock(state_path, wait=LOCK_WAIT):
    """Hold an OS file lock on the session while its state is read, uploaded and saved. Yields False when another hook
    still holds it after `wait` seconds (that one, or the next Stop, sends the new lines). The OS releases the lock
    if the process dies."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(state_path.with_name(state_path.name + '.lock'), 'a+b')
    locked = False
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        yield locked
    finally:
        if locked:
            try:
                if os.name == 'nt':
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        stream.close()


def _next_line_end(stream, start, size):
    """Offset just after the first newline at or after `start`, or None if that record is not fully written yet."""
    stream.seek(start)
    position = start
    while position < size:
        block = stream.read(1024 * 1024)
        if not block:
            break
        found = block.find(b'\n')
        if found >= 0:
            return position + found + 1
        position += len(block)
    return None


def _prune(folder):
    """Forget sessions idle for STATE_KEEP_DAYS whose transcript is gone. A session that can still be resumed keeps its
    offset and sequence: restarting from zero would reuse sequence numbers the server already stored, and it would
    take the new lines for duplicates."""
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    try:
        items = list((Path(folder) / 'loader' / 'sharing-state').iterdir())
    except OSError:
        return
    for item in items:
        try:
            if item.suffix != '.json' or item.stat().st_mtime >= cutoff:
                continue
            transcript = _load_state(item)['path']
            if transcript and Path(transcript).exists():
                continue
            item.unlink()
            item.with_name(item.name + '.lock').unlink(missing_ok=True)
        except OSError:
            pass


def _upload(account, agent, session_id, seq, events):
    body = {'agent': agent, 'session_id': session_id, 'seq': seq, 'events': events}
    headers = {'X-E3D-App-Version': package_version(), 'X-E3D-Loader-Version': LOADER_VERSION}
    status = 0
    for attempt in range(2):
        token = account.access_token(force_refresh=attempt == 1)
        status, _ = account.http.request('POST', '/api/v1/telemetry/transcript', json_body=body, token=token, headers=headers, timeout=30)
        if status != 401:
            break
    return status


def _hook():
    payload = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace'))
    folder = data_dir()
    if sharing_state(folder) == 'off':
        return
    path, session_id = payload.get('transcript_path'), payload.get('session_id')
    if not isinstance(path, str) or not path or not isinstance(session_id, str) or not session_id:
        return
    agent = detect_agent(path, payload)
    time.sleep(float(os.environ.get('E3D_SHARING_FLUSH_WAIT', FLUSH_WAIT)))
    state_path = _state_file(folder, agent, session_id)
    with _session_lock(state_path) as locked:
        if locked:
            _send_new(folder, agent, session_id, Path(path), state_path)


def _send_new(folder, agent, session_id, transcript, state_path):
    if sharing_state(folder) == 'off':  # turned off while this hook waited for the flush or the lock
        return
    state = _load_state(state_path)
    state['path'] = str(transcript)
    size = transcript.stat().st_size
    offset = state['offset'] if state['offset'] <= size else 0
    with open(transcript, 'rb') as stream:
        while True:
            stream.seek(offset)
            data = stream.read(MAX_READ)
            end = data.rfind(b'\n')
            if end >= 0:
                break
            if offset + len(data) >= size:
                return  # the host is still writing this line
            # one record larger than MAX_READ (a huge tool result or image): skip it instead of stopping here forever
            skip_to = _next_line_end(stream, offset + len(data), size)
            if skip_to is None:
                return
            offset = state['offset'] = skip_to
            _save_state(state_path, state)
    since = shared_since(folder)
    lines, position = [], offset
    for raw in data[:end].split(b'\n'):
        position += len(raw) + 1
        events = parse_line(agent, raw)
        if since is not None:  # written while sharing was off (or of unknown time): never sent
            events = [event for event in events if (event_time(event) or 0) >= since]
        lines.append((position, events))
    if not state['qualified']:
        text = ' '.join('%s %s' % (event.get('name', ''), event['text']) for _, events in lines for event in events)
        if not RELEVANT.search(text):
            state['offset'] = position
            _save_state(state_path, state)
            return
        state['qualified'] = True
    config = load_config()
    account = Account(config, folder, Http(config['server_url']))
    batch, batch_end, sent = [], state['offset'], 0

    def flush():
        nonlocal batch, sent
        if batch:
            if sharing_state(folder) == 'off':  # turned off during this upload: send nothing more
                raise RuntimeError('sharing was turned off')
            status = _upload(account, agent, session_id, state['seq'], batch)
            if status != 200:
                raise RuntimeError('upload refused (HTTP %d)' % status)
            state['seq'] += 1
            sent += len(batch)
        state['offset'] = batch_end
        _save_state(state_path, state)
        batch = []
    try:
        for end_offset, events in lines:
            batch.extend(events)
            batch_end = end_offset
            if len(batch) >= BATCH_EVENTS or sum(len(e['text']) for e in batch) >= BATCH_BYTES:
                flush()
        flush()
    except Exception as exc:
        _save_state(state_path, {**state, 'qualified': True})  # what was sent stays sent; the rest is retried by the next Stop
        _outcome(folder, ok=False, sent=sent, error=str(exc)[:300])
        raise
    _outcome(folder, ok=True, sent=sent, agent=agent)
    _prune(folder)


def run_hook(argv=None):
    """Entry point of the Stop hook. Never raises and prints nothing: a failure here must not disturb the user's session."""
    try:
        _hook()
    except Exception:
        if os.environ.get('E3D_SHARING_DEBUG'):
            import traceback
            traceback.print_exc()
    return 0
