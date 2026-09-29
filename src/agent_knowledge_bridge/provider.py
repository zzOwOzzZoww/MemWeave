"""User-owned review provider settings; no third-party dependency or secret logging."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .paths import memweave_home


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        if os.name != 'nt':
            os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    method = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    # UI_FORBIDDEN; deliberately not LOCAL_MACHINE: only the current user.
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('无法访问当前用户的 API 凭据，请重新运行 memweave setup')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


def normalize_url(value: str) -> str:
    value = value.strip().rstrip('/')
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {'https', 'http'} or not parsed.hostname:
        raise ValueError('Base URL 必须是完整的 https:// 地址（本地服务可用 http://）')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Base URL 不可包含凭据、查询参数或片段')
    if parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ValueError('远程 API 请使用 HTTPS；HTTP 仅支持本机回环地址')
    if value.endswith('/chat/completions'):
        value = value[:-len('/chat/completions')]
    return value


def _saved() -> dict:
    path = memweave_home() / 'provider.json'
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('模型配置不可识别，请重新运行 memweave setup')
    return value


def settings() -> dict[str, Any]:
    saved = _saved()
    if saved:
        secret = saved.get('credential', {})
        if secret.get('storage') == 'windows-dpapi':
            if os.name != 'nt':
                raise RuntimeError('Windows 凭据不能迁移到其他系统，请重新配置 API Key')
            key = _crypt(base64.b64decode(secret['value']), decrypt=True).decode('utf-8')
        else:
            key = str(secret.get('value') or '')
        return {'base_url': saved['base_url'], 'model': saved['model'], 'api_key': key,
                'source': 'user-config', 'storage': secret.get('storage', 'none')}
    # Legacy environment remains supported until the user explicitly runs setup.
    return {
        'base_url': os.getenv('MW_BASE_URL') or os.getenv('OPENAI_BASE_URL') or 'https://api.deepseek.com/v1',
        'model': os.getenv('MW_MODEL') or os.getenv('OPENAI_MODEL') or 'deepseek-flash',
        'api_key': os.getenv('MW_API_KEY') or os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_API_KEY') or '',
        'source': 'environment', 'storage': 'environment',
    }


def public_settings() -> dict:
    value = settings()
    return {key: value[key] for key in ('base_url', 'model', 'source', 'storage')} | {
        'configured': bool(value['api_key']), 'key_configured': bool(value['api_key'])}


def save(*, base_url: str, model: str, api_key: str | None = None) -> dict:
    base_url = normalize_url(base_url)
    model = model.strip()
    if not model or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise ValueError('请填写服务商提供的完整模型名称')
    previous = settings()
    if not api_key and base_url != previous['base_url']:
        raise ValueError('更换 API 地址时必须重新输入 Key，避免把旧服务商凭据发给新地址')
    key = api_key.strip() if api_key else previous['api_key']
    if not key or any(ord(c) < 32 for c in key):
        raise ValueError('API Key 不能为空或含控制字符')
    credential = {'storage': 'file-mode-600', 'value': key}
    if os.name == 'nt':
        credential = {'storage': 'windows-dpapi', 'value': base64.b64encode(_crypt(key.encode())).decode('ascii')}
    atomic_json(memweave_home() / 'provider.json', {
        'version': 1, 'base_url': base_url, 'model': model, 'credential': credential})
    return public_settings()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a credential to a redirected host.


def open_request(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def check_connection() -> dict:
    value = settings()
    if not value['api_key']:
        raise ValueError('请先配置 API Key')
    request = urllib.request.Request(normalize_url(value['base_url']) + '/chat/completions',
        data=json.dumps({'model': value['model'], 'messages': [{'role': 'user', 'content': 'Reply OK.'}],
                         'max_tokens': 8}).encode(),
        headers={'Authorization': 'Bearer ' + value['api_key'], 'Content-Type': 'application/json'})
    try:
        with open_request(request, timeout=15) as response:
            result = json.load(response)
        if not isinstance(result, dict) or not result.get('choices'):
            raise ValueError('服务响应不符合 Chat Completions 格式')
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'API 检查失败（HTTP {exc.code}），请检查地址、Key、模型与额度') from None
    except urllib.error.URLError:
        raise RuntimeError('API 网络连接失败，请检查地址及网络') from None
    return {'ok': True, 'model': value['model']}
