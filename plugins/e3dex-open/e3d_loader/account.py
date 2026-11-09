"""Device login (RFC 8628) or, for the free plugin, automatic PC linking; token storage and the online check."""
import hashlib
import os
import platform
import sys
import time
import uuid
import webbrowser

from . import LOADER_VERSION, LoaderError, secure_store

GRANT_DEVICE = 'urn:ietf:params:oauth:grant-type:device_code'

MESSAGES = {
    'LOGIN_REQUIRED': 'E3DEX 로그인이 필요합니다. e3d_account(action="login")으로 로그인하세요.',
    'ACCESS_REVOKED': '관리자가 이 계정 또는 PC의 E3DEX 사용 권한을 해제했습니다. 관리자에게 문의하세요.',
    'NOT_APPROVED': '관리자 승인 대기 중입니다. 승인 후 다시 시도하세요.',
    'PAYMENT_REQUIRED': 'E3DEX를 사용하려면 Paddle 정기 구독 결제가 필요합니다.',
    'FREE_ACCESS_ENDED': ('E3DEX 무료 이용이 종료되었습니다. 계속 쓰려면 같은 마켓플레이스에서 구독용 E3DEX 플러그인(e3dex)을 '
                          '설치하고 로그인하세요.'),
}
# The free plugin has no sign-in: these replace the sign-in wording.
FREE_MESSAGES = {
    'LOGIN_REQUIRED': '이 PC를 E3DEX 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.',
    'ACCESS_REVOKED': '관리자가 이 PC의 E3DEX 사용을 차단했습니다. 관리자에게 문의하세요.',
}


def machine_id(folder):
    """A hash identifying this PC for the free plugin (the Windows machine id itself is never sent)."""
    raw = None
    if sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography', 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                raw = 'windows:' + str(winreg.QueryValueEx(key, 'MachineGuid')[0]).strip().lower()
        except OSError:
            raw = None
    if not raw:  # not Windows, or the registry value is unreadable: a random id kept in the data folder
        path = folder / 'machine-id'
        try:
            raw = path.read_text(encoding='ascii').strip()
        except OSError:
            raw = ''
        if not raw:
            raw = 'random:' + uuid.uuid4().hex
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding='ascii')
    # Fixed salt from the plugin's first name (e3dex-free): changing it would give every PC a new id
    # and lift the administrator's blocks.
    return hashlib.sha256(('e3dex-' + 'free\0' + raw).encode('utf-8')).hexdigest()


def device_name():
    if sys.platform == 'win32':
        return '%s (%s)' % (os.environ.get('COMPUTERNAME', 'Windows PC'), os.environ.get('USERNAME', 'user'))
    return platform.node() or 'PC'


def open_browser(url):
    if os.environ.get('E3D_LOADER_NO_BROWSER'):
        return False
    try:
        if sys.platform == 'win32':
            os.startfile(url)  # noqa: S606 - opens the default browser for the user's own login page
            return True
        return bool(webbrowser.open(url))
    except Exception:
        return False


class Account:
    def __init__(self, config, data_dir, http):
        self.config = config
        self.http = http
        self.folder = data_dir / 'loader'
        self.free = config.get('access') == 'free'
        # Separate files, so the free and the paid plugin on one PC never use each other's tokens.
        self.token_path = self.folder / ('free-auth.json' if self.free else 'auth.json')
        self.pending_path = self.folder / 'login.json'

    # ------------------------------------------------------------ storage
    def _tokens(self):
        data = secure_store.load(self.token_path)
        if data and data.get('server_url') == self.config['server_url'] and data.get('access_token'):
            return data
        return None

    def clear(self):
        secure_store.delete(self.token_path)
        secure_store.delete(self.pending_path)

    def _fail(self, code, description=None):
        if code == 'ACCESS_REVOKED':
            self.clear()
        message = (FREE_MESSAGES.get(code) if self.free else None) or MESSAGES[code]
        if description and code not in ('LOGIN_REQUIRED', 'FREE_ACCESS_ENDED'):
            message = description
        details = {}
        if code == 'PAYMENT_REQUIRED':
            details['billing_url'] = self.config['server_url'] + '/billing'
            message += ' 결제: ' + details['billing_url']
        raise LoaderError(code, message, details)

    def refused(self, status, payload):
        """Raise the matching LoaderError for a refused API call (HTTP 401, 402 or 403)."""
        if status == 402:
            self._fail('PAYMENT_REQUIRED')
        if status == 403:
            error = payload.get('error')
            if error == 'free_access_disabled':
                self._fail('FREE_ACCESS_ENDED')
            self._fail('ACCESS_REVOKED' if error == 'access_revoked' else 'NOT_APPROVED', payload.get('error_description'))
        if status == 401:
            self._fail('LOGIN_REQUIRED')

    def billing(self):
        if self.free:
            return {'status': 'NOT_NEEDED', 'message': '무료 버전은 로그인과 결제가 필요 없습니다.'}
        url = self.config['server_url'] + '/billing'
        return {'status': 'BILLING', 'billing_url': url, 'browser_opened': open_browser(url),
                'message': '브라우저에서 로그인한 뒤 Paddle 정기 구독 결제 또는 구독 관리를 진행하세요. 결제 후 기존 작업을 다시 요청하세요.'}

    # ------------------------------------------------------------ online check
    def _refresh(self, tokens, timeout):
        status, payload = self.http.request('POST', '/oauth/token', timeout=timeout, form={
            'grant_type': 'refresh_token', 'refresh_token': tokens.get('refresh_token', ''), 'client_id': self.config['client_id']})
        if status == 200 and payload.get('access_token'):
            tokens = {**tokens, 'access_token': payload['access_token'],
                      'access_expires_at': int(time.time()) + int(payload.get('expires_in', 3600))}
            secure_store.save(self.token_path, tokens)
            return tokens
        description = payload.get('error_description')
        if status == 402 or payload.get('error') == 'payment_required':
            self._fail('PAYMENT_REQUIRED')
        if description == 'free_access_disabled':
            self._fail('FREE_ACCESS_ENDED')
        if description == 'access_revoked':
            self._fail('ACCESS_REVOKED')
        if description == 'not_approved':
            self._fail('NOT_APPROVED')
        if status in (400, 401):
            secure_store.delete(self.token_path)
            if self.free:  # e.g. the PC was disconnected: link it again
                return self._register(timeout)
            self._fail('LOGIN_REQUIRED')
        raise LoaderError('SERVER_ERROR', '관리 서버 응답 오류 (HTTP %d). 잠시 후 다시 시도하세요.' % status)

    def access_token(self, force_refresh=False, timeout=15):
        """A currently valid access token (refreshed when close to expiry)."""
        tokens = self._tokens() or self._first_tokens(timeout)
        if force_refresh or tokens.get('access_expires_at', 0) - 60 < time.time():
            tokens = self._refresh(tokens, timeout)
        return tokens['access_token']

    def session(self, app_version=None, timeout=10):
        """Ask the server, every time, whether this PC may run E3DEX. Returns (session, tokens)."""
        tokens = self._tokens() or self._first_tokens(timeout)
        if tokens.get('access_expires_at', 0) - 60 < time.time():
            tokens = self._refresh(tokens, timeout)
        for attempt in range(2):
            status, payload = self.http.request('GET', '/api/v1/session', token=tokens['access_token'], timeout=timeout,
                                                headers={'X-E3D-App-Version': app_version, 'X-E3D-Loader-Version': LOADER_VERSION})
            if status == 200 and isinstance(payload.get('user'), dict):
                return payload, tokens
            if status == 401 and attempt == 0:
                tokens = self._refresh(tokens, timeout)
                continue
            self.refused(status, payload)
            raise LoaderError('SERVER_ERROR', '관리 서버 응답 오류 (HTTP %d). 잠시 후 다시 시도하세요.' % status)
        self._fail('LOGIN_REQUIRED')

    # ------------------------------------------------------------ free plugin: link this PC without sign-in
    def _first_tokens(self, timeout):
        if self.free:
            return self._register(timeout)
        self._fail('LOGIN_REQUIRED')

    def _register(self, timeout=15):
        status, payload = self.http.request('POST', '/api/v1/free/register', timeout=timeout, form={
            'client_id': self.config['client_id'], 'device_name': device_name(), 'loader_version': LOADER_VERSION,
            'machine_id': machine_id(self.folder)})
        if status == 200 and payload.get('access_token'):
            tokens = {'server_url': self.config['server_url'], 'access_token': payload['access_token'],
                      'access_expires_at': int(time.time()) + int(payload.get('expires_in', 3600)),
                      'refresh_token': payload.get('refresh_token', ''), 'user': payload.get('user', {})}
            secure_store.save(self.token_path, tokens)
            return tokens
        error = payload.get('error')
        if error == 'free_access_disabled':
            self._fail('FREE_ACCESS_ENDED')
        if error == 'access_revoked':
            self._fail('ACCESS_REVOKED', payload.get('error_description'))
        if status == 429:
            raise LoaderError('SERVER_ERROR', '이 인터넷 주소에서 PC 연결 요청이 너무 많습니다. 잠시 후 다시 시도하세요.')
        raise LoaderError('SERVER_ERROR', 'E3DEX 서버에 이 PC를 연결하지 못했습니다 (HTTP %d). 잠시 후 다시 시도하세요.' % status)

    # ------------------------------------------------------------ device login
    def _pending(self):
        data = secure_store.load(self.pending_path)
        if data and data.get('server_url') == self.config['server_url'] and data.get('expires_at', 0) > time.time() + 5:
            return data
        return None

    def _start(self):
        status, payload = self.http.request('POST', '/oauth/device_authorization', form={
            'client_id': self.config['client_id'], 'device_name': device_name(), 'loader_version': LOADER_VERSION})
        if status != 200 or not payload.get('device_code'):
            raise LoaderError('SERVER_ERROR', '로그인을 시작할 수 없습니다 (HTTP %d).' % status, payload)
        pending = {
            'server_url': self.config['server_url'], 'device_code': payload['device_code'], 'user_code': payload['user_code'],
            'verification_uri': payload['verification_uri'], 'verification_uri_complete': payload['verification_uri_complete'],
            'expires_at': int(time.time()) + int(payload.get('expires_in', 900)), 'interval': int(payload.get('interval', 5)),
        }
        secure_store.save(self.pending_path, pending)
        return pending

    def _poll(self, pending):
        status, payload = self.http.request('POST', '/oauth/token', form={
            'grant_type': GRANT_DEVICE, 'device_code': pending['device_code'], 'client_id': self.config['client_id']})
        if status == 200 and payload.get('access_token'):
            tokens = {'server_url': self.config['server_url'], 'access_token': payload['access_token'],
                      'access_expires_at': int(time.time()) + int(payload.get('expires_in', 3600)),
                      'refresh_token': payload.get('refresh_token', ''), 'user': payload.get('user', {})}
            secure_store.save(self.token_path, tokens)
            secure_store.delete(self.pending_path)
            return 'ok', tokens
        return payload.get('error', 'server_error'), payload.get('error_description', '')

    def login(self, wait_seconds=40, on_start=None):
        """Start or resume a device login and wait up to wait_seconds for the user (and admin)."""
        if self.free:
            session, _ = self.session()
            return {'status': 'LOGGED_IN', 'user': session['user'],
                    'message': '무료 버전은 로그인 없이 자동으로 연결됩니다. 이 PC는 연결되어 있습니다.'}
        try:
            session, _ = self.session()
            return {'status': 'LOGGED_IN', 'user': session['user'], 'message': '이미 로그인되어 있습니다.'}
        except LoaderError as exc:
            if exc.code == 'PAYMENT_REQUIRED':
                return {**self.billing(), 'status': 'PAYMENT_REQUIRED'}
            if exc.code not in ('LOGIN_REQUIRED', 'ACCESS_REVOKED', 'NOT_APPROVED'):
                raise
        pending = self._pending()
        opened = False
        if pending is None:
            pending = self._start()
            opened = open_browser(pending['verification_uri_complete'])
        if on_start:
            on_start(pending)
        deadline = time.time() + wait_seconds
        interval = pending['interval']
        detail = 'awaiting_user'
        while True:
            result, value = self._poll(pending)
            if result == 'ok':
                return {'status': 'LOGGED_IN', 'user': value.get('user', {}), 'message': '로그인되었습니다.'}
            if result == 'access_denied':
                secure_store.delete(self.pending_path)
                if value == 'access_revoked':
                    return {'status': 'ACCESS_REVOKED', 'message': MESSAGES['ACCESS_REVOKED']}
                return {'status': 'DENIED', 'message': '브라우저에서 연결을 거부했습니다. 다시 로그인하려면 login을 다시 호출하세요.'}
            if result in ('expired_token', 'invalid_grant'):
                secure_store.delete(self.pending_path)
                return {'status': 'EXPIRED', 'message': '로그인 요청이 만료되었습니다. login을 다시 호출하면 새 코드가 발급됩니다.'}
            if result == 'slow_down':
                interval += 5
            elif result == 'authorization_pending':
                detail = value or detail
            else:
                raise LoaderError('SERVER_ERROR', '로그인 확인 중 서버 오류: %s' % result)
            if time.time() + interval > deadline:
                break
            time.sleep(interval)
        waiting_admin = detail == 'awaiting_admin_approval'
        if detail == 'awaiting_payment':
            return {'status': 'PAYMENT_REQUIRED', 'billing_url': self.config['server_url'] + '/billing',
                    'message': MESSAGES['PAYMENT_REQUIRED'] + ' 결제 후 login을 다시 호출하세요.'}
        return {
            'status': 'WAITING_FOR_ADMIN' if waiting_admin else 'WAITING_FOR_USER',
            'verification_uri': pending['verification_uri'],
            'verification_uri_complete': pending['verification_uri_complete'],
            'user_code': pending['user_code'],
            'browser_opened': opened,
            'expires_in': max(0, int(pending['expires_at'] - time.time())),
            'message': ('PC 연결 요청은 받았고 관리자 승인을 기다리는 중입니다. 관리자가 승인한 뒤 login을 다시 호출하세요.' if waiting_admin else
                        '사용자가 브라우저에서 위 주소를 열고, 로그인한 뒤 코드 %s 를 확인해 "연결하기"를 눌러야 합니다. '
                        '계정이 없으면 같은 화면에서 사용 신청을 할 수 있습니다. 완료 후 login을 다시 호출하세요.' % pending['user_code']),
        }

    def logout(self):
        tokens = self._tokens()
        if tokens:
            try:
                self.http.request('POST', '/oauth/revoke', timeout=8, form={
                    'token': tokens.get('refresh_token') or tokens['access_token'], 'client_id': self.config['client_id']})
            except LoaderError:
                pass
        self.clear()
        if self.free:
            return {'status': 'LOGGED_OUT', 'message': '이 PC 연결을 해제했습니다. 다음에 E3DEX 도구를 쓰면 자동으로 다시 연결됩니다.'}
        return {'status': 'LOGGED_OUT', 'message': '로그아웃했습니다. 이 PC 연결도 서버에서 해제되었습니다.'}
