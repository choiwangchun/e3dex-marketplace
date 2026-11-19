"""First-use upgrade of the installed E3D add-in for the Windows plugin."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import LoaderError, ROOT, package_version

WAIT = 'NEEDS_E3D_CLOSED'
MESSAGES = {
    WAIT: '작업을 마친 뒤 E3D Design·Draw를 정상 종료하세요. 대기 중인 업데이트가 자동으로 DLL을 교체합니다.',
    'UPDATING': 'E3D 연결 DLL을 업데이트하는 중입니다. Windows 관리자 승인 창이 표시되면 확인하세요.',
    'NEEDS_ADMIN': 'DLL 교체에 관리자 승인이 필요합니다. 권한을 확인한 뒤 E3DEX 초기 설정으로 다시 시도하세요.',
    'REVIEW_DLL_VERSION': '설치된 DLL이 더 최신이거나 같은 버전의 다른 빌드입니다. 설치 버전을 확인하세요.',
    'NEEDS_E3D_PATH': '업데이트 대상 E3D 설치 폴더를 결정할 수 없습니다. 초기 설정에서 정확한 경로를 선택하세요.',
}


def setup(action, root=None):
    shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    cmd = [str(shell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
           str(ROOT / 'scripts/setup.ps1'), '-ExistingDllOnly', '-Action', action]
    if root:
        cmd.extend(['-E3DRoot', str(root)])
    if action == 'apply':
        cmd.append('-Elevate')
    run = subprocess.run(cmd, capture_output=True, encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW,
                         timeout=90 if action == 'inspect' else None)
    data = json.loads(run.stdout.lstrip('\ufeff'))
    if run.returncode or data['status'] == 'FAILED':
        raise LoaderError('DLL_UPDATE_FAILED', data.get('error') or 'E3D 설치 점검에 실패했습니다.')
    return data


def receipt(folder, root):
    key = hashlib.sha256(os.path.normcase(str(Path(root).resolve())).encode()).hexdigest()[:20]
    return folder / 'dll-updates' / (key + '-' + package_version() + '.json')


def save(path, result):
    tmp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05)
    finally:
        tmp.unlink(missing_ok=True)


def load(path):
    try:
        return json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}


def worker_lock(path):
    # Windows releases this byte-range lock even if the watcher is interrupted.
    import msvcrt
    stream = path.with_suffix('.lock').open('a+b')
    try:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        stream.close()
        return None
    return stream


def run_worker(folder, root, gate):
    path = receipt(folder, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = worker_lock(path)
    if lock is None:
        return
    with lock:
        try:
            while True:
                result = setup('inspect', root)
                if result['status'] == WAIT:
                    save(path, result)
                    wait_for_exit(result['running'])
                    continue
                if result['status'] == 'NEEDS_INSTALL':
                    gate()
                    save(path, {'status': 'UPDATING'})
                    result = setup('apply', root)
                    if result['status'] == WAIT:
                        continue
                break
        except (OSError, ValueError, subprocess.SubprocessError, LoaderError) as exc:
            result = {'status': getattr(exc, 'code', 'DLL_UPDATE_FAILED'), 'message': str(exc)}
        save(path, result)


def wait_for_exit(processes):
    # Open handles refer to this process instance. A new Design/Draw process is
    # discovered by the next inspection and again by the installer before replacement.
    from ctypes import WinDLL, get_last_error
    from ctypes.wintypes import BOOL, DWORD, HANDLE
    kernel = WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    kernel.OpenProcess.restype = HANDLE
    kernel.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    kernel.CloseHandle.argtypes = [HANDLE]
    for process in processes:
        handle = kernel.OpenProcess(0x00100000, False, process['Id'])
        if handle:
            try:
                kernel.WaitForSingleObject(handle, 0xffffffff)
            finally:
                kernel.CloseHandle(handle)
        elif get_last_error() != 87:  # already exited
            time.sleep(3)
            break


def raise_status(result):
    code = result['status']
    raise LoaderError(code, result.get('message') or MESSAGES.get(code, 'E3D DLL 설치 상태를 확인하세요.'), result)


def ensure_current(folder):
    if sys.platform != 'win32':
        return
    try:
        result = setup('inspect')
        status = result['status']
        if status in ('INSTALLED', 'NO_EXISTING_DLL'):
            return
        if status not in (WAIT, 'NEEDS_INSTALL'):
            # Leave normal first-time discovery available when no E3D is installed.
            if status == 'NEEDS_E3D_PATH' and not result.get('candidates'):
                return
            raise_status(result)
        root = result['e3d_root']
        path = receipt(folder, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = worker_lock(path)
        if lock is not None:
            with lock:
                previous = load(path)
                if previous.get('status') in ('NEEDS_ADMIN', 'REVIEW_DLL_VERSION', 'DLL_UPDATE_FAILED'):
                    raise_status(previous)
                save(path, {'status': WAIT if status == WAIT else 'UPDATING'})
            # The child independently takes the lock and re-inspects before writing.
            subprocess.Popen([sys.executable, '-B', '-u', str(ROOT / 'scripts/launch.py'), '_upgrade_dll', root],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        if status == WAIT:
            raise_status(result)
        # Finish an ordinary immediate upgrade in the original tool call. UAC can
        # take longer; the independent worker then continues after the call returns.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = load(path)
            if state.get('status') in ('UPDATED', 'INSTALLED'):
                if setup('inspect', root)['status'] == 'INSTALLED':
                    return
            elif state.get('status') not in (None, WAIT, 'UPDATING'):
                raise_status(state)
            time.sleep(0.25)
        raise_status({'status': 'UPDATING'})
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise LoaderError('DLL_UPDATE_FAILED', str(exc)) from exc
