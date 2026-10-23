"""Export a reviewed source snapshot. Never publishes or rewrites private history."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = Path(__file__).resolve().parent / 'templates'
PREFIXES = ('extension/', 'x4lan/', 'native/', 'distribution/', 'dev/', 'tests/')
ROOT_FILES = {'VERSION.txt', 'VERSIONING.txt', 'requirements-dev.txt', 'prepare.py'}
TEXT_SUFFIXES = {'.py', '.lua', '.xml', '.c', '.h', '.ps1', '.sh', '.cmd', '.json', '.txt', '.md', '.html'}
BINARIES = {'extension/x4_lan_lab/native/x4lan_net.so', 'extension/x4_lan_lab/native/x4lan_net_64.dll'}
PRIVATE_NAMES = {'JOIN-SESSION.json', 'session.json', 'installation.json', 'installation-state.json', '.env'}
FIXTURE_REPLACEMENTS = {'192.0.2.109': '192.0.2.109', '192.0.2.202': '192.0.2.202', '70000001': '70000001'}
FIXTURE_FILES = {'tests/test_internal_sessions.py', 'tests/test_windows_profiles.ps1',
                 'tests/test_game_adapter.py', 'tests/test_public.py', 'tests/test_distribution.py'}
PATTERNS = {
    'provider credential': r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[baprs]-[A-Za-z0-9-]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{30,}|AKIA[0-9A-Z]{16})',
    'private key': r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'private coordination URL': r'https://[^\s\x22\x27<>]*slack\.com/',
    'personal local path': r'(?:/home|/var/home)/bazzite(?:/|\b)',
    'private network': r'10\.55\.4\.[0-9]+',
    'personal game-profile example': r'70000001',
    'credential literal': r'''(?i)(?:token|password|secret|api_key|session_key)[\x22\x27]?\s*[:=]\s*[\x22\x27][A-Za-z0-9_+/=.-]{20,}[\x22\x27]''',
    'credential in URL': r'https?://[^\s/@:]+:[^\s/@]+@',
    'email address': r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
}

def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])

def selected(path):
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).is_absolute() or '..' in parts or '\\' in path:
        raise ValueError('Non-portable or escaping source path: ' + path)
    if any(part in {'.git', '.local', '.venv', 'logs', 'backups', 'runtime', 'save'} for part in parts):
        raise ValueError('Private/generated path in source inventory: ' + path)
    if parts[-1] in PRIVATE_NAMES or parts[-1].endswith('-session.json'):
        raise ValueError('Private session file in source inventory: ' + path)
    if path in BINARIES:
        return False
    if path in ROOT_FILES or path.startswith(PREFIXES):
        if PurePosixPath(path).suffix not in TEXT_SUFFIXES:
            raise ValueError('Unreviewed source file type: ' + path)
        return True
    return False

def check_text(path, data):
    text = data.decode('utf-8')
    if '\0' in text:
        raise ValueError('Binary data in source snapshot: ' + path)
    for category, pattern in PATTERNS.items():
        if re.search(pattern, text):
            raise ValueError(category + ' requires review in ' + path)
    return text

def transform(path, data):
    if path in FIXTURE_FILES:
        for old, new in FIXTURE_REPLACEMENTS.items():
            data = data.replace(old.encode(), new.encode())
    check_text(path, data)
    return data

def digest(data):
    return hashlib.sha256(data).hexdigest()

def export(revision, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Refusing to replace an existing publication candidate.')
    commit = git('rev-parse', '--verify', revision + '^{commit}').decode().strip()
    version = git('show', commit + ':VERSION.txt').decode().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Unexpected source version.')
    output, records, excluded = {}, [], []
    for row in git('ls-tree', '-rz', '--full-tree', commit).split(b'\0'):
        if not row:
            continue
        header, raw_path = row.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        path = raw_path.decode()
        if not selected(path):
            excluded.append(path)
            continue
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError('Symlink/submodule or unsupported mode: ' + path)
        original = git('cat-file', 'blob', oid)
        data = transform(path, original)
        output[path] = (data, int(mode[-3:], 8))
        records.append({'path': path, 'source_sha256': digest(original),
                        'published_sha256': digest(data), 'mode': mode,
                        'change': 'synthetic test fixture identifiers' if original != data else 'none'})
    config = output['extension/x4_lan_lab/ui/config.lua'][0].decode()
    if not re.search(r'\benabled\s*=\s*false\b', config) or not re.search(r'\btoken\s*=\s*""', config):
        raise ValueError('Source configuration is not inert/credential-free.')
    for path in sorted(TEMPLATES.iterdir()):
        if not path.is_file() or path.is_symlink():
            raise ValueError('Unexpected publication template: ' + path.name)
        data = path.read_bytes().replace(b'{{VERSION}}', version.encode())
        check_text(path.name, data)
        if path.name in output:
            raise ValueError('Template would replace original source: ' + path.name)
        output[path.name] = (data, 0o644)
    manifest = {'format': 1, 'version': version, 'purpose': 'Source inspection only; not a playable release.',
                'source_commit': commit, 'original_source_files': records,
                'omitted_categories': ['private Git history and metadata', 'internal reports and handoffs',
                                       'compiled native libraries', 'session/save/runtime data',
                                       'private collaboration and publication tooling'],
                'snapshot_files': [{'path': p, 'sha256': digest(data), 'mode': format(mode, 'o')}
                                   for p, (data, mode) in sorted(output.items())]}
    output['SOURCE-MANIFEST.json'] = ((json.dumps(manifest, indent=2) + '\n').encode(), 0o644)
    destination.mkdir(parents=True, exist_ok=False)
    for path, (data, mode) in output.items():
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)
    return {'source_commit': commit, 'files': len(output), 'original_files': len(records),
            'unchanged_original_files': sum(r['change'] == 'none' for r in records),
            'changed_test_fixtures': [r['path'] for r in records if r['change'] != 'none'],
            'excluded_files': excluded, 'bytes': sum(len(data) for data, _ in output.values())}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--destination', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(export(args.revision, args.destination), indent=2))
