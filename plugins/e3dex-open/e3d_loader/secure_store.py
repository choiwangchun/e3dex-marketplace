"""Small JSON secret files. On Windows the content is encrypted with DPAPI (current user)."""
import base64
import json
import os
from pathlib import Path
import sys
import uuid


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_wchar_p if protect else ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    result = Blob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if not function(ctypes.byref(source), 'E3DEX' if protect else None, None, None, None, flags, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), 'DPAPI call failed')
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode('utf-8')
    payload = {'format': 'plain', 'data': value}
    if sys.platform == 'win32':
        try:
            sealed = _dpapi(raw, True)
            if _dpapi(sealed, False) == raw:
                payload = {'format': 'dpapi', 'data': base64.b64encode(sealed).decode('ascii')}
        except Exception:
            pass
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream)
    os.replace(tmp, path)


def load(path):
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        if payload.get('format') == 'dpapi':
            return json.loads(_dpapi(base64.b64decode(payload['data']), False).decode('utf-8'))
        if payload.get('format') == 'plain' and isinstance(payload.get('data'), dict):
            return payload['data']
    except Exception:
        pass
    return None


def delete(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
