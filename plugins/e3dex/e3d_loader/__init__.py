"""E3DEX plugin loader: approved-account check and calls to the server-side core."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

LOADER_VERSION = '2.1.0'
ROOT = Path(__file__).resolve().parents[1]


class LoaderError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def version_key(version):
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r'[.+-]', str(version or '0')))


def package_version():
    try:
        return json.loads((ROOT / 'plugin.json').read_text(encoding='utf-8'))['version']
    except (OSError, ValueError, KeyError):
        return None


def load_config():
    path = Path(os.environ.get('E3D_LOADER_CONFIG') or ROOT / 'e3d_loader' / 'config.json')
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise LoaderError('CONFIG_ERROR', 'Plugin config is missing or invalid; reinstall the plugin: %s' % exc)
    url = str(config.get('server_url', '')).rstrip('/')
    parts = urlsplit(url)
    local = parts.hostname in ('127.0.0.1', 'localhost', '::1')
    if parts.scheme != 'https' and not (parts.scheme == 'http' and local):
        raise LoaderError('CONFIG_ERROR', 'The approval server must use https: ' + url)
    return {'server_url': url, 'client_id': config.get('client_id', 'e3dex')}


def data_dir():
    value = os.environ.get('E3D_BRIDGE_DATA_DIR')
    if not value:
        if not os.environ.get('LOCALAPPDATA'):
            raise LoaderError('CONFIG_ERROR', 'LOCALAPPDATA is not set')
        value = str(Path(os.environ['LOCALAPPDATA']) / 'E3DBridge')
    return Path(value)
