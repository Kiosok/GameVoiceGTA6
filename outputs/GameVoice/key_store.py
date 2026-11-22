"""Current-user Windows DPAPI storage. Never prints or logs credentials."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data, protect):
    source = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming = _Blob(len(data), source)
    outgoing = _Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    args = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    fn.argtypes = args
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError('Windows не смогла ' + ('защитить' if protect else 'прочитать') + ' сохранённый ключ.')
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.memset(outgoing.data, 0, outgoing.size)
        kernel.LocalFree(outgoing.data)
        ctypes.memset(source, 0, len(data))


def key_path():
    return Path(os.environ['LOCALAPPDATA']) / 'GameVoice' / 'api-key.dpapi'


def save_key(key, path=None):
    key = key.strip()
    if not key or '\n' in key or '\r' in key:
        raise ValueError('Введите API-ключ одной строкой.')
    encrypted = _crypt(key.encode('utf-8'), True)
    target = Path(path) if path is not None else key_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.pending')
    temporary.write_bytes(encrypted)
    os.replace(temporary, target)


def load_key(path=None):
    target = Path(path) if path is not None else key_path()
    try:
        return _crypt(target.read_bytes(), False).decode('utf-8')
    except (OSError, ValueError, UnicodeError):
        return ''


def forget_key(path=None):
    target = Path(path) if path is not None else key_path()
    target.unlink(missing_ok=True)
