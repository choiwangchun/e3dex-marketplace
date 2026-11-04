"""Device login (RFC 8628), token storage and the online approval check."""
import os
import platform
import sys
import time
import webbrowser

from . import LOADER_VERSION, LoaderError, secure_store

GRANT_DEVICE = 'urn:ietf:params:oauth:grant-type:device_code'

MESSAGES = {
    'LOGIN_REQUIRED': 'E3DEX 로그인이 필요합니다. e3d_account(action="login")으로 로그인하세요.',
    'ACCESS_REVOKED': '관리자가 이 계정 또는 PC의 E3DEX 사용 권한을 해제했습니다. 관리자에게 문의하세요.',
    'NOT_APPROVED': '관리자 승인 대기 중입니다. 승인 후 다시 시도하세요.',
}


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
        self.token_path = self.folder / 'auth.json'
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
        message = MESSAGES[code]
        if description and code != 'LOGIN_REQUIRED':
            message = description
        raise LoaderError(code, message)

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
        if description == 'access_revoked':
            self._fail('ACCESS_REVOKED')
        if description == 'not_approved':
            self._fail('NOT_APPROVED')
        if status in (400, 401):
            secure_store.delete(self.token_path)
            self._fail('LOGIN_REQUIRED')
        raise LoaderError('SERVER_ERROR', '관리 서버 응답 오류 (HTTP %d). 잠시 후 다시 시도하세요.' % status)

    def access_token(self, force_refresh=False, timeout=15):
        """A currently valid access token (refreshed when close to expiry)."""
        tokens = self._tokens()
        if not tokens:
            self._fail('LOGIN_REQUIRED')
        if force_refresh or tokens.get('access_expires_at', 0) - 60 < time.time():
            tokens = self._refresh(tokens, timeout)
        return tokens['access_token']

    def session(self, app_version=None, timeout=10):
        """Ask the server, every time, whether this PC may run E3DEX. Returns (session, tokens)."""
        tokens = self._tokens()
        if not tokens:
            self._fail('LOGIN_REQUIRED')
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
            if status == 403:
                self._fail('ACCESS_REVOKED' if payload.get('error') == 'access_revoked' else 'NOT_APPROVED',
                           payload.get('error_description'))
            if status == 401:
                self._fail('LOGIN_REQUIRED')
            raise LoaderError('SERVER_ERROR', '관리 서버 응답 오류 (HTTP %d). 잠시 후 다시 시도하세요.' % status)
        self._fail('LOGIN_REQUIRED')

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
        try:
            session, _ = self.session()
            return {'status': 'LOGGED_IN', 'user': session['user'], 'message': '이미 로그인되어 있습니다.'}
        except LoaderError as exc:
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
        return {'status': 'LOGGED_OUT', 'message': '로그아웃했습니다. 이 PC 연결도 서버에서 해제되었습니다.'}
