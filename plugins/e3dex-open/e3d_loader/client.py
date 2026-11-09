"""HTTPS client for the approval server. Never follows redirects (keeps tokens on one host)."""
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from . import LOADER_VERSION, LoaderError


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Redirects are not allowed', headers, fp)


def tls_context():
    """Normal certificate verification without Python 3.13's extra strict checks.

    Python 3.13 turned on VERIFY_X509_STRICT. HTTPS-inspecting security products and company proxies
    often re-sign traffic with certificates that lack extensions such as Authority Key Identifier;
    browsers accept them, Python 3.13 rejects them ("Missing Authority Key Identifier"). Only those
    extra checks are turned off: the chain must still end at a trusted root (Windows certificate store)
    and the host name must still match.
    """
    context = ssl.create_default_context()
    context.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)
    return context


class Http:
    def __init__(self, server_url):
        self.server_url = server_url
        self.opener = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=tls_context()))

    def request(self, method, path, form=None, json_body=None, token=None, headers=None, timeout=15,
                max_response=2 * 1024 * 1024):
        """Return (status, json_payload)."""
        if json_body is not None:
            data, content_type = json.dumps(json_body, ensure_ascii=False).encode('utf-8'), 'application/json'
        elif form is not None:
            data, content_type = urllib.parse.urlencode(form).encode('ascii'), 'application/x-www-form-urlencoded'
        else:
            data, content_type = None, None
        req = urllib.request.Request(self.server_url + path, data=data, method=method)
        req.add_header('User-Agent', 'E3DEXLoader/' + LOADER_VERSION)
        req.add_header('Accept', 'application/json')
        if content_type:
            req.add_header('Content-Type', content_type)
        if token:
            req.add_header('Authorization', 'Bearer ' + token)
        for name, value in (headers or {}).items():
            if value:
                req.add_header(name, value)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                body = resp.read(max_response + 1)
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(65536)
            except Exception:
                body = b''
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, 'reason', exc)
            if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(exc, ssl.SSLCertVerificationError):
                raise LoaderError('SERVER_UNREACHABLE',
                                  '관리 서버(%s)의 보안 인증서를 확인할 수 없습니다: %s. 회사 보안 프로그램이나 프록시가 HTTPS를 검사하는 경우 '
                                  '생길 수 있습니다. 이 PC의 브라우저에서 위 주소가 열리는지 확인하고, 계속되면 이 메시지를 관리자에게 전달하세요.'
                                  % (self.server_url, getattr(reason, 'verify_message', None) or reason),
                                  {'reason': str(reason)[:300], 'kind': 'certificate'})
            raise LoaderError('SERVER_UNREACHABLE',
                              '관리 서버(%s)에 연결할 수 없습니다. 인터넷 연결을 확인하세요. 서버 확인 없이는 E3DEX를 사용할 수 없습니다.' % self.server_url,
                              {'reason': str(getattr(exc, 'reason', exc))[:300]})
        if len(body) > max_response:
            raise LoaderError('SERVER_ERROR', '서버 응답이 너무 큽니다.')
        try:
            payload = json.loads(body.decode('utf-8')) if body else {}
        except ValueError:
            payload = {}
        return status, payload if isinstance(payload, dict) else {}
