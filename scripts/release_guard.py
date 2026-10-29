#!/usr/bin/env python3
"""Fail-closed guard for the gated release delivery job.

Extracted from the former inline heredoc in ``.github/workflows/verify-release.yml``
so the exact executed code is unit-testable (``tests/test_release_guard.py``)
instead of a YAML-embedded copy. Hard properties, each backed by tests:

- publishes only an EXISTING tag that points at the EXACT tested commit
  (``--verify-tag``; the guard never creates, moves or deletes tags);
- tag version == wheel filename version == sdist filename version ==
  in-package METADATA/PKG-INFO version (PEP 440 X.Y.Z[-{a,b,rc}N] forms);
- the dist inventory is exactly wheel, sdist and the versioned hash-locked
  requirements export the wheel is installed with (every requirement
  carries ``--hash=sha256:``; no editable, path, VCS or self entries);
- local artifact integrity: SHA256SUMS must cover exactly the three dist
  files and match freshly computed digests (pure stdlib, no coreutils);
- exact readback: the post-create view must carry the EXACT requested
  tagName, and the RAW asset inventory must match exactly (name set with
  no duplicates, mandatory well-formed sizes, digests when served)
  before any dict conversion; as the decisive check, re-downloaded
  asset BYTES must equal the local files;
- every ambiguity fails closed: a non-zero exit with an explicit reason,
  never a retry, overwrite or any destructive side effect.

The guard talks to GitHub exclusively through the ``gh`` CLI driven by the
explicit environment transport (``GH_TOKEN``/``GH_REPO``/``SOURCE_SHA``/
``RELEASE_TAG``) supplied by the workflow. It never prints file contents,
tokens or private literals - only tag names, SHAs and asset filenames.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile

PACKAGE = 'quoth'          # distribution name in pyproject.toml
PACKAGE_FILE = PACKAGE.replace('-', '_')  # wheel/sdist filename stem

_WHEEL_RE = re.compile(rf'{PACKAGE_FILE}-(?P<ver>[^-]+)-py3-none-any\.whl')
_SDIST_RE = re.compile(rf'{PACKAGE_FILE}-(?P<ver>.+)\.tar\.gz')
_REQS_RE = re.compile(rf'{PACKAGE_FILE}-(?P<ver>[^-]+)-requirements\.txt')
_TAG_RE = re.compile(r'v(?P<ver>[0-9A-Za-z.\-+]+)')
_STRICT_RE = re.compile(r'(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?')
_PRE_CANON = {'alpha': 'a', 'beta': 'b', 'preview': 'rc', 'pre': 'rc',
              'c': 'rc', 'rc': 'rc', 'a': 'a', 'b': 'b'}
_SHA_RE = re.compile(r'[0-9a-f]{40}')


class GuardError(SystemExit):
    """Fail-closed exit carrying a printable, non-secret reason."""


def fail(reason: str) -> 'GuardError':
    return GuardError(f'release-guard: {reason}')


def normalize_version(raw: str) -> str:
    """Normalize the supported PEP 440 subset; reject anything else.

    Supported: ``X.Y.Z`` and ``X.Y.Z-<pre>N`` (pre in alpha/beta/rc spellings),
    matching tag ``v0.1.0``/``v0.1.0-rc1`` against wheel ``0.1.0``/``0.1.0rc1``.
    Epochs, post/dev releases and local versions are refused explicitly -
    failing closed on unusual forms is safer than guessing equality.
    """
    v = raw.strip().lower()
    if not v or re.search(r'[+!]|post|dev|local', v):
        raise ValueError(f'unsupported version form: {raw!r}')
    if '-' in v:
        head, _, tail = v.rpartition('-')
        m = re.fullmatch(r'(alpha|beta|preview|pre|rc|c|a|b)[.]?(\d+)', tail)
        if not m:
            raise ValueError(f'unsupported pre-release form: {raw!r}')
        v = head + _PRE_CANON[m.group(1)] + m.group(2)
    m = _STRICT_RE.fullmatch(v)
    if not m or any(part != '0' and part.startswith('0') for part in m.groups()[:3]):
        raise ValueError(f'unsupported version form: {raw!r}')
    return v


def _parse_metadata(data: bytes, source: str) -> dict:
    headers: dict[str, list[str]] = {}
    for line in data.decode('utf-8', 'replace').splitlines():
        if not line.strip():
            break
        if ':' in line:
            key, _, value = line.partition(':')
            headers.setdefault(key.strip().lower(), []).append(value.strip())
    for key in ('name', 'version'):
        if len(headers.get(key, [])) != 1:
            raise ValueError(f'{source}: expected exactly one {key} header')
    return {'name': headers['name'][0], 'version': headers['version'][0]}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _parse_sums(text: str) -> dict[str, str]:
    entries = {}
    for line in text.splitlines():
        m = re.fullmatch(r'([0-9a-f]{64})[ \t]+\*?(\S.*)', line.strip())
        if not m:
            raise ValueError(f'malformed SHA256SUMS line {len(entries) + 1}')
        entries[m.group(2)] = m.group(1)
    return entries


def _check_requirements(text: str, name: str) -> None:
    """A ``uv export --locked`` with hashes; refuse anything not pinned by digest."""
    logical, current = [], ''
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        current += ' ' + stripped.rstrip('\\').strip()
        if not stripped.endswith('\\'):
            logical.append(current.strip())
            current = ''
    if current:
        logical.append(current.strip())
    if not logical:
        raise fail(f'{name} lists no requirements')
    for entry in logical:
        head = entry.split()[0]
        if (head.startswith(('-', '.', '/')) or ':' in head.split(';')[0]
                or '@' in entry.split('--hash')[0]):
            raise fail(f'{name} contains an unpinned, path, VCS or option entry')
        project = re.split(r'[=<>!~;\[ ]', head, maxsplit=1)[0]
        if re.sub(r'[-_.]+', '-', project).lower() == PACKAGE:
            raise fail(f'{name} must not list {PACKAGE} itself')
        if '==' not in head or not re.search(r'--hash=sha256:[0-9a-f]{64}', entry):
            raise fail(f'{name} has a requirement without exact version and sha256 hash')


def plan_release(root: Path, tag: str, source_sha: str) -> dict:
    """Validate everything locally checkable; return the delivery plan.

    Raises GuardError (exit 1) on any violation. No network access.
    """
    tag = (tag or '').strip()
    if not tag:
        raise fail('RELEASE_TAG is required')
    m = _TAG_RE.fullmatch(tag)
    if not m or not tag.startswith('v'):
        raise fail(f'invalid tag {tag!r}: expected vMAJOR.MINOR.PATCH[-preN]')
    try:
        tag_version = normalize_version(m.group('ver'))
    except ValueError as exc:
        raise fail(f'invalid tag {tag!r}: {exc}')

    source_sha = (source_sha or '').strip().lower()
    if not _SHA_RE.fullmatch(source_sha):
        raise fail('SOURCE_SHA must be a 40-hex commit id')

    dist = root / 'dist'
    if not dist.is_dir():
        raise fail(f'{dist} is missing: no verified artifacts')
    files = sorted(p for p in dist.iterdir())
    if len(files) != 3:
        raise fail(f'expected exactly wheel, sdist and requirements in dist/, found '
                   f'{[p.name for p in files]}')
    wheel = sdist = reqs = None
    for p in files:
        if not p.is_file():
            raise fail(f'dist/ entry {p.name!r} is not a regular file')
        if p.name.endswith('.whl'):
            if wheel is not None:
                raise fail('more than one wheel in dist/')
            wheel = p
        elif p.name.endswith('.tar.gz'):
            if sdist is not None:
                raise fail('more than one sdist in dist/')
            sdist = p
        elif p.name.endswith('-requirements.txt'):
            if reqs is not None:
                raise fail('more than one requirements export in dist/')
            reqs = p
        else:
            raise fail(f'unexpected artifact {p.name!r} in dist/')
    if wheel is None or sdist is None or reqs is None:
        raise fail('dist/ must contain exactly wheel, sdist and requirements')

    wm, sm = _WHEEL_RE.fullmatch(wheel.name), _SDIST_RE.fullmatch(sdist.name)
    rm = _REQS_RE.fullmatch(reqs.name)
    if not wm or not sm or not rm:
        raise fail(f'unexpected artifact names {wheel.name!r}/{sdist.name!r}/{reqs.name!r}: '
                   f'expected {PACKAGE_FILE}-<ver>-py3-none-any.whl, '
                   f'{PACKAGE_FILE}-<ver>.tar.gz and {PACKAGE_FILE}-<ver>-requirements.txt')
    versions = {
        'wheel filename': wm.group('ver'),
        'sdist filename': sm.group('ver'),
        'requirements filename': rm.group('ver'),
    }
    try:
        normalized = {k: normalize_version(v) for k, v in versions.items()}
    except ValueError as exc:
        raise fail(f'artifact version {exc}; tag claims {tag_version}')
    for label, ver in normalized.items():
        if ver != tag_version:
            raise fail(f'{label} version {ver} does not equal tag version '
                       f'{tag_version} (tag {tag})')

    # In-package identity: the bytes inside must claim the same name+version
    # as the filenames and the tag (wrong artifact source detection).
    with zipfile.ZipFile(wheel) as zf:
        meta_names = [n for n in zf.namelist() if re.fullmatch(r'[^/]+\.dist-info/METADATA', n)]
        if len(meta_names) != 1:
            raise fail('wheel must contain exactly one dist-info/METADATA')
        wheel_meta = _parse_metadata(zf.read(meta_names[0]), wheel.name)
    with tarfile.open(sdist, 'r:gz') as tf:
        pkg_names = [m.name for m in tf.getmembers()
                     if re.fullmatch(r'[^/]+/PKG-INFO', m.name)]
        if len(pkg_names) != 1:
            raise fail('sdist must contain exactly one PKG-INFO')
        pkg_info = tf.extractfile(pkg_names[0])
        if pkg_info is None:
            raise fail('sdist PKG-INFO is not a regular member')
        sdist_meta = _parse_metadata(pkg_info.read(), sdist.name)
    for label, meta, filename_ver in (('wheel METADATA', wheel_meta, wm.group('ver')),
                                      ('sdist PKG-INFO', sdist_meta, sm.group('ver'))):
        norm_name = re.sub(r'[-_.]+', '-', meta['name']).lower()
        if norm_name != PACKAGE:
            raise fail(f'{label} names distribution {meta["name"]!r}, expected {PACKAGE!r}')
        try:
            meta_ver = normalize_version(meta['version'])
        except ValueError as exc:
            raise fail(f'{label} version {exc}')
        if meta_ver != tag_version:
            raise fail(f'{label} version {meta_ver} does not equal tag version '
                       f'{tag_version} (filename claimed {filename_ver})')

    try:
        reqs_text = reqs.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        raise fail(f'{reqs.name} is not UTF-8 text')
    _check_requirements(reqs_text, reqs.name)

    # Source binding: the artifacts must record the exact tested commit.
    commit_file = root / 'artifacts' / 'source-commit.txt'
    if not commit_file.is_file():
        raise fail('artifacts/source-commit.txt is missing')
    recorded = commit_file.read_text().strip().lower()
    if recorded != source_sha:
        raise fail('artifact source mismatch: source-commit.txt does not '
                   'equal SOURCE_SHA')

    # Checksum coverage and integrity, independent of coreutils.
    sums_file = root / 'artifacts' / 'SHA256SUMS'
    if not sums_file.is_file():
        raise fail('artifacts/SHA256SUMS is missing')
    try:
        sums = _parse_sums(sums_file.read_text())
    except ValueError as exc:
        raise fail(f'SHA256SUMS is invalid: {exc}')
    expected_paths = {f'dist/{p.name}' for p in (wheel, sdist, reqs)}
    if set(sums) != expected_paths:
        raise fail(f'SHA256SUMS must cover exactly {sorted(expected_paths)}, '
                   f'found {sorted(sums)}')
    digests = {}
    for p in (wheel, sdist, reqs):
        digest = _sha256(p)
        if sums[f'dist/{p.name}'] != digest:
            raise fail(f'checksum mismatch for dist/{p.name}: SHA256SUMS '
                       'does not equal the freshly computed digest')
        digests[p.name] = digest

    return {
        'tag': tag,
        'version': tag_version,
        'source_sha': source_sha,
        'expected_assets': sorted([wheel.name, sdist.name, reqs.name]),
        'files': {'wheel': str(wheel), 'sdist': str(sdist), 'requirements': str(reqs)},
        'paths': {p.name: str(p) for p in (wheel, sdist, reqs)},
        'sizes': {p.name: p.stat().st_size for p in (wheel, sdist, reqs)},
        'sha256': digests,
    }


class Gh:
    """Thin explicit transport over the ``gh`` CLI (injectable in tests)."""

    def __init__(self, repo: str):
        self.repo = repo
        self.log: list[list[str]] = []

    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        self.log.append(list(argv))
        return subprocess.run(list(argv), capture_output=True, text=True)

    def tag_commit(self, tag: str) -> str:
        res = subprocess.run(
            ['gh', 'api', f'repos/{self.repo}/commits/{tag}', '--jq', '.sha'],
            capture_output=True, text=True)
        self.log.append([f'api:commits:{tag}'])
        if res.returncode != 0:
            raise fail(f'tag {tag} is not readable: {_trim(res.stderr)} '
                       '(the guard never creates tags)')
        return res.stdout.strip().lower()

    def release_view(self, tag: str) -> tuple[str, dict | None]:
        res = self._run('gh', 'release', 'view', tag, '--repo', self.repo,
                        '--json', 'tagName,assets')
        if res.returncode == 0:
            try:
                return 'exists', json.loads(res.stdout)
            except json.JSONDecodeError:
                return 'error', {'stderr': 'unparseable release view output'}
        low = res.stderr.lower()
        if 'not found' in low or '404' in low:
            return 'missing', None
        return 'error', {'stderr': _trim(res.stderr)}

    def release_create(self, tag: str, files: list[str], title: str, notes: str):
        return self._run('gh', 'release', 'create', tag, '--repo', self.repo,
                         '--verify-tag', '--title', title, '--notes', notes, *files)

    def release_download(self, tag: str, dest: str):
        return self._run('gh', 'release', 'download', tag, '--repo', self.repo,
                         '--dir', dest)


def _trim(text: str, limit: int = 300) -> str:
    return ' '.join((text or '').split())[:limit]


def deliver(plan: dict, gh: Gh) -> dict:
    """Publish per the plan and verify the exact publication outcome."""
    tag, sha = plan['tag'], plan['source_sha']
    commit = gh.tag_commit(tag)
    if commit != sha:
        raise fail(f'tag {tag} points at {commit}, not the exact tested '
                   f'commit {sha}')
    status, info = gh.release_view(tag)
    if status == 'exists':
        raise fail(f'release {tag} already exists; refusing to overwrite')
    if status == 'error':
        raise fail(f'cannot establish that release {tag} is absent '
                   f"({(info or {}).get('stderr', 'unknown error')}); "
                   'refusing to publish')

    res = gh.release_create(
        tag, [plan['files']['wheel'], plan['files']['sdist'], plan['files']['requirements']],
        f'quoth {tag}', f'Exact source {sha}; synthetic verification only.')
    if res.returncode != 0:
        raise fail(f'release create failed (rc={res.returncode}): '
                   f'{_trim(res.stderr)} — outcome unknown, inspect manually; '
                   'never blind-retry')

    status, info = gh.release_view(tag)
    if status != 'exists':
        raise fail(f'ambiguous publication outcome: release {tag} is not '
                   f'readable after create ({status}); inspect manually')
    # Exact identity first: the publication we just verified by readback
    # must be THE requested release, not something the API happens to
    # return. Absent identity is unknown identity and fails closed too.
    readback = info or {}
    readback_tag = readback.get('tagName')
    if not isinstance(readback_tag, str) or readback_tag != tag:
        raise fail(f'post-create readback reports tagName {readback_tag!r} '
                   f'instead of the exact requested tag {tag!r}; inspect '
                   'manually, never blind-retry')
    # Assert the RAW asset inventory before any dict conversion so a
    # duplicated or unnamed entry cannot silently disappear.
    raw_assets = readback.get('assets')
    if not isinstance(raw_assets, list) or not all(
            isinstance(a, dict) for a in raw_assets):
        raise fail('post-create readback is malformed: assets must be a '
                   'list of objects')
    names = [a.get('name') for a in raw_assets]
    if any(not isinstance(n, str) or not n for n in names):
        raise fail('post-create readback is malformed: every asset must '
                   'carry a non-empty name')
    if len(names) != len(set(names)):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise fail(f'post-create readback reports duplicate asset names '
                   f'{duplicates}; refusing ambiguous inventory')
    if sorted(names) != plan['expected_assets']:
        raise fail(f'readback asset names {sorted(names)} differ from '
                   f"expected {plan['expected_assets']}")
    assets = dict(zip(names, raw_assets))
    for name, meta in assets.items():
        size = meta.get('size')
        if not isinstance(size, int) or isinstance(size, bool):
            raise fail(f'readback size for {name} is missing or malformed '
                       f'({size!r}); unknown size is not verified size')
        if size != plan['sizes'][name]:
            raise fail(f'readback size for {name} is {size}, local is '
                       f"{plan['sizes'][name]}")
        digest = meta.get('digest')
        if digest is not None:
            algo, _, hexd = digest.partition(':')
            if algo.lower() != 'sha256' or hexd.lower() != plan['sha256'][name]:
                raise fail(f'readback digest for {name} does not match the '
                           'built artifact')

    with tempfile.TemporaryDirectory(prefix='release-guard-') as td:
        res = gh.release_download(tag, td)
        if res.returncode != 0:
            raise fail(f'release download verification failed: {_trim(res.stderr)}')
        downloaded = {p.name: _sha256(p) for p in Path(td).iterdir() if p.is_file()}
        if sorted(downloaded) != plan['expected_assets']:
            raise fail(f'downloaded assets {sorted(downloaded)} differ from '
                       f"expected {plan['expected_assets']}")
        for name, digest in downloaded.items():
            if digest != plan['sha256'][name]:
                raise fail(f'downloaded bytes of {name} are NOT byte-identical '
                           'to the verified artifact')

    receipt = {'published': tag, 'source_commit': sha,
               'assets': plan['sha256'],
               'readback': 'exact names, sizes, digests and downloaded bytes'}
    print(json.dumps(receipt, indent=2))
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Fail-closed guard for gated release delivery.')
    parser.add_argument('--root', default='verified',
                        help='directory containing dist/ and artifacts/ '
                             'from the verify job (default: verified)')
    args = parser.parse_args(argv)
    repo = os.environ.get('GH_REPO', '').strip()
    source_sha = os.environ.get('SOURCE_SHA', '')
    tag = os.environ.get('RELEASE_TAG', '')
    if not repo:
        raise fail('GH_REPO is required')
    if not os.environ.get('GH_TOKEN'):
        raise fail('GH_TOKEN is required (explicit env transport)')
    planned = plan_release(Path(args.root), tag, source_sha)
    deliver(planned, Gh(repo))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except GuardError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
