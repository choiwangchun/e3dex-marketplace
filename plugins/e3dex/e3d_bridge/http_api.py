"""Authenticated loopback JSON adapter for the common application service."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import sys
from .contract import catalog
from .errors import BridgeError
from .service import Service, envelope, parse_json, ROOT

MAX_BODY = 1024 * 1024


def status_code(result):
    if result['ok']:
        return 200
    code = result['error']['code']
    if code == 'UNAUTHORIZED':
        return 401
    if code in ('PROJECT_NOT_ALLOWED', 'READ_ONLY', 'ORIGIN_REJECTED'):
        return 403
    if code in ('SESSION_NOT_FOUND', 'NOT_FOUND', 'UNKNOWN_OPERATION'):
        return 404
    if code in ('SESSION_BUSY', 'TARGET_CHANGED', 'STALE_SESSION', 'LISTENER_MISMATCH'):
        return 409
    if code in ('BRIDGE_UNAVAILABLE', 'SESSION_UNAVAILABLE'):
        return 503
    if code in ('INTERNAL_ERROR', 'INDETERMINATE', 'EXECUTION_FAILED'):
        return 500
    return 400


def handler(service, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, format, *args):
            pass  # Never log tokens or request bodies.

        def send(self, result):
            body = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.send_response(status_code(result))
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Never replay an operation after client disconnect.

        def authorized(self):
            if self.headers.get('Origin'):
                raise BridgeError('ORIGIN_REJECTED', 'Browser-origin requests are not accepted')
            if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                raise BridgeError('UNAUTHORIZED', 'A local API bearer token is required')

        def do_GET(self):
            try:
                self.authorized()
                if self.path != '/v1/schema':
                    raise BridgeError('NOT_FOUND', 'Use GET /v1/schema or POST /v1/call')
                self.send(envelope(data=catalog()))
            except BridgeError as exc:
                self.send(envelope(error=exc))

        def do_POST(self):
            request = None
            try:
                self.authorized()
                if self.path != '/v1/call':
                    raise BridgeError('NOT_FOUND', 'Use POST /v1/call')
                if self.headers.get('Transfer-Encoding'):
                    raise BridgeError('INVALID_REQUEST', 'Chunked request bodies are not supported')
                if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    raise BridgeError('INVALID_REQUEST', 'Content-Type must be application/json')
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    length = 0
                if not 0 < length <= MAX_BODY:
                    raise BridgeError('INVALID_REQUEST', 'Body must be between 1 and 1048576 bytes')
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise BridgeError('INVALID_REQUEST', 'Incomplete request body')
                request = parse_json(raw)
                self.send(service.call(request))
            except BridgeError as exc:
                self.send(envelope(request, error=exc))
            except (OSError, ValueError) as exc:
                self.send(envelope(request, error=BridgeError('INVALID_REQUEST', str(exc))))

        def unsupported(self):
            self.send(envelope(error=BridgeError('INVALID_REQUEST', 'Use GET /v1/schema or POST /v1/call')))

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = unsupported
    return Handler


def create_server(port, token, service=None):
    if len(token) < 32:
        raise BridgeError('CONFIG_ERROR', 'API token must contain at least 32 characters')
    server = ThreadingHTTPServer(('127.0.0.1', port), handler(service or Service(), token))
    server.daemon_threads = False
    return server


def owner_token_file():
    import os
    from pathlib import Path
    if os.name == 'nt':
        # The former relay token could have inherited read access from a shared install.
        # Use an owner-only token in the profile, for the same-user native bridge.
        import subprocess
        path = Path(os.environ['LOCALAPPDATA']) / 'AVEVA/E3DBridge/api-owner-token.txt'
        path.parent.mkdir(parents=True, exist_ok=True)
        script = r"""
$ErrorActionPreference='Stop'
$p=$env:E3DEX_OWNER_TOKEN_PATH
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
if (![IO.File]::Exists($p)) {
    $acl=New-Object Security.AccessControl.FileSecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true,$false)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','Allow')))
    $f=[IO.FileStream]::new($p,[IO.FileMode]::CreateNew,[Security.AccessControl.FileSystemRights]::Write,
        [IO.FileShare]::None,4096,[IO.FileOptions]::None,$acl)
    try {
        $bytes=New-Object byte[] 48
        $rng=[Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $out=[Text.Encoding]::ASCII.GetBytes([Convert]::ToBase64String($bytes))
        $f.Write($out,0,$out.Length)
    } finally { $f.Dispose() }
}
$acl=[IO.File]::GetAccessControl($p)
if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value -or !$acl.AreAccessRulesProtected) {
    throw 'Invalid owner token ACL'
}
foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
    if ($rule.IdentityReference.Value -ne $sid.Value) { throw 'Shared owner token ACL' }
}
"""
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', script], check=True,
                       env={**os.environ, 'E3DEX_OWNER_TOKEN_PATH': str(path)})
        return path

    path = Path(os.environ.get('E3D_BRIDGE_DATA_DIR', Path.home() / '.e3dex')) / 'api-owner-token.txt'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_EXCL | os.O_CREAT, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise BridgeError('CONFIG_ERROR', 'The local API token must be private to its owner')
    else:
        with os.fdopen(fd, 'w', encoding='ascii') as f:
            f.write(secrets.token_urlsafe(48))
    return path


def serve(port=None):
    api = json.loads((ROOT / 'bridge.json').read_text(encoding='utf-8-sig'))['api']
    if api.get('host') != '127.0.0.1':
        raise BridgeError('CONFIG_ERROR', 'The JSON API must bind to 127.0.0.1')
    token_path = owner_token_file()
    server = create_server(api['port'] if port is None else port, token_path.read_text(encoding='utf-8').strip())
    print(json.dumps({'event': 'listening', 'url': f'http://127.0.0.1:{server.server_port}',
                      'token_file': str(token_path)}), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
