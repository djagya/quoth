"""Import-free execution of isolated functions from the pinned read-only WebUI source.

This does not start WebUI or make an HTTP request; browser/Compose acceptance is
independent. Point ``QUOTH_WEBUI_SOURCE`` at the WebUI checkout's ``api`` directory to run
these; without it (as on CI) they skip, which is not a proxy PASS claim.
"""
import ast
import os
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import unquote

import pytest

SOURCE = Path(os.environ.get('QUOTH_WEBUI_SOURCE') or '/nonexistent-webui-source')


def isolated(file, names, namespace):
    tree = ast.parse((SOURCE / file).read_text())
    definitions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in definitions} == set(names)
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
                              *definitions], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SOURCE / file), 'exec'), namespace)
    return namespace


@pytest.mark.skipif(not (SOURCE / 'extensions.py').is_file(), reason='pinned WebUI source unavailable')
def test_pinned_proxy_consent_auth_rotation_and_encoded_path(monkeypatch):
    class ProxyError(Exception):
        def __init__(self, reason, status):
            self.status = status
            super().__init__(reason)
    auth = ModuleType('api.auth')
    auth.is_auth_enabled = lambda: True
    token = ModuleType('api.extension_sidecar_auth')
    token.current_token = lambda _id: 'synthetic-token-v1'
    token.ensure_token = lambda _id: None
    api = ModuleType('api'); api.__path__ = []; api.extension_sidecar_auth = token
    monkeypatch.setitem(sys.modules, 'api', api)
    monkeypatch.setitem(sys.modules, 'api.auth', auth)
    monkeypatch.setitem(sys.modules, 'api.extension_sidecar_auth', token)
    state = {'consented': True}
    ns = dict(Optional=str, Dict=dict, Any=object, unquote=unquote,
        ExtensionSidecarProxyError=ProxyError, _valid_extension_id=lambda x: x == 'quoth',
        _extension_root=lambda: Path('/synthetic'), _new_diagnostics=dict,
        _load_extension_state=lambda _d: {'disabled_extensions': [], 'sidecar_proxy_consents': {'quoth': 'http://127.0.0.1:17889'}},
        _load_manifest_with_status=lambda _r, _d: ({}, {'loaded': True}),
        _manifest_extension_state=lambda *_a, **_k: {'known_ids': {'quoth'}},
        _extension_sidecar_records=lambda *_a, **_k: ([], {'quoth': {
            'sidecar': {'origin': 'http://127.0.0.1:17889', 'proxy_auth': 'token-v1'},
            'proxy': {'path': '/', 'available': True, 'consented': state['consented']}}}))
    isolated('extensions.py', ['_fully_unquote_path', '_is_valid_sidecar_proxy_path',
             '_normalize_sidecar_proxy_path', 'resolve_extension_sidecar_proxy_target'], ns)
    resolve = ns['resolve_extension_sidecar_proxy_target']
    for path in ('../private', '%252e%252e/private', '//external', 'api/%2e%2e/private', 'api?secret'):
        with pytest.raises(ProxyError) as error:
            resolve('quoth', path)
        assert error.value.status == 400
    assert resolve('quoth', 'api/memory/v1/status')['auth_token'] == 'synthetic-token-v1'
    state['consented'] = False
    with pytest.raises(ProxyError) as error:
        resolve('quoth', 'api/memory/v1/status')
    assert error.value.status == 403
    state['consented'] = True
    auth.is_auth_enabled = lambda: False
    with pytest.raises(ProxyError) as error:
        resolve('quoth', 'api/memory/v1/status')
    assert error.value.status == 403
    auth.is_auth_enabled = lambda: True
    token.current_token = lambda _id: None
    with pytest.raises(ProxyError) as error:
        resolve('quoth', 'api/memory/v1/status')
    assert error.value.status == 503


@pytest.mark.skipif(not (SOURCE / 'routes.py').is_file(), reason='pinned WebUI source unavailable')
def test_pinned_proxy_origin_csrf_and_header_strip():
    class Handler:
        def __init__(self, headers):
            self.headers = headers
    ns = dict(_re=re, os=SimpleNamespace(getenv=lambda *_a: ''),
        _clear_csrf_failure_reason=lambda h: None,
        _set_csrf_failure_reason=lambda h, _reason: False,
        _normalize_host_port=lambda h: tuple((h.split(':') + [None])[:2]),
        _ports_match=lambda _scheme, a, b: a == b,
        _allowed_public_origins=lambda: set(),
        _connection_bound_header_names=lambda _h: set())
    isolated('routes.py', ['_check_same_origin_browser_request',
             '_extension_sidecar_proxy_request_headers'], ns)
    check = ns['_check_same_origin_browser_request']
    assert not check(Handler({'Host': 'memory.local'}), require_provenance=True)
    assert not check(Handler({'Host': 'memory.local', 'Origin': 'https://evil.local'}), require_provenance=True)
    assert not check(Handler({'Host': 'memory.local', 'Origin': 'https://memory.local',
                              'Sec-Fetch-Site': 'cross-site'}), require_provenance=True)
    assert check(Handler({'Host': 'memory.local', 'Origin': 'https://memory.local'}), require_provenance=True)
    forwarded = ns['_extension_sidecar_proxy_request_headers'](Handler({
        'Authorization': 'private', 'Cookie': 'session', 'X-Hermes-Sidecar-Token': 'forged',
        'X-CSRF-Token': 'forged', 'Origin': 'https://memory.local', 'Content-Type': 'application/json'}))
    assert forwarded == {'Content-Type': 'application/json'}
