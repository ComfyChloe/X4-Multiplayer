"""Private, versioned host checkpoints, opt-in and not wired to gameplay.

Owns only its new repository directory. Save completion/host authority must be
established by the caller. A checkpoint can be transported using x4lan.seed;
this module does not load X4 or claim safe resumption of persisted MD roles.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets

from . import seed, world_start, save_origin


class WorldStoreError(seed.SeedError):
    pass


def _sync_dir(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write(path, value):
    with path.open('xb') as stream:
        stream.write(seed.canonical(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > seed.MAX_HEADER:
        raise WorldStoreError('invalid repository metadata file')
    return json.loads(path.read_bytes())


@contextmanager
def _lock(root):
    path = root / '.writer.lock'
    if path.is_symlink():
        raise WorldStoreError('linked writer lock')
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise WorldStoreError('another host checkpoint writer is active') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class WorldStore:
    def __init__(self, root, *, world_id):
        self.root = Path(root)
        if self.root.is_symlink() or not self.root.is_dir():
            raise WorldStoreError('repository must be a real directory')
        self.recipe = world_start.validate(_read(self.root / 'world.json'))
        if self.recipe['world_id'] != world_id:
            raise WorldStoreError('repository belongs to another world')
        self.recipe_digest = world_start.fingerprint(self.recipe)
        if (self.root / 'checkpoints').is_symlink() or not (self.root / 'checkpoints').is_dir():
            raise WorldStoreError('invalid checkpoint directory')

    @classmethod
    def create(cls, root, recipe):
        recipe = world_start.validate(recipe)
        root = Path(root)
        root.mkdir()  # Never adopt an existing directory or someone else's files.
        (root / 'checkpoints').mkdir()
        _write(root / 'world.json', recipe)
        _write(root / 'HEAD.json', dict(checkpoint=None, sha256=None))
        _sync_dir(root)
        _sync_dir(root.parent)
        return cls(root, world_id=recipe['world_id'])

    def _unchanged(self):
        if world_start.fingerprint(_read(self.root / 'world.json')) != self.recipe_digest:
            raise WorldStoreError('world recipe changed while repository was open')

    def latest(self):
        """Return a validated committed snapshot, or None for an empty world.

        Never guess the latest from directory order or adopt orphaned writes.
        Corrupt committed state fails closed, rather than silently rolling back.
        """
        self._unchanged()
        head = self.root / 'HEAD.json'
        pointer = _read(head)
        if pointer == {'checkpoint': None, 'sha256': None}:
            return None
        if (not isinstance(pointer, dict) or set(pointer) != {'checkpoint', 'sha256'}
                or not seed._hex(pointer['checkpoint'], seed.HEX32)
                or not seed._hex(pointer['sha256'], seed.HEX64)):
            raise WorldStoreError('invalid committed checkpoint pointer')
        folder = self.root / 'checkpoints' / pointer['checkpoint']
        if folder.is_symlink():
            raise WorldStoreError('linked checkpoint directory')
        info = _read(folder / 'checkpoint.json')
        if hashlib.sha256(seed.canonical(info)).hexdigest() != pointer['sha256']:
            raise WorldStoreError('checkpoint metadata checksum mismatch')
        keys = {'protocol', 'world_id', 'revision', 'checkpoint', 'parent', 'recipe_sha256', 'save_sha256', 'native_origin'}
        if (not isinstance(info, dict) or set(info) != keys or type(info['protocol']) is not int
                or info['protocol'] != 1 or info['world_id'] != self.recipe['world_id']
                or type(info['revision']) is not int or not 1 <= info['revision'] <= 2**53-1
                or info['checkpoint'] != pointer['checkpoint'] or info['recipe_sha256'] != self.recipe_digest
                or not seed._hex(info['save_sha256'], seed.HEX64)
                or (info['parent'] is not None and not seed._hex(info['parent'], seed.HEX64))
                or (info['revision'] == 1) != (info['parent'] is None)):
            raise WorldStoreError('invalid committed checkpoint metadata')
        manifest, snapshot = seed.read_bundle(folder)
        if (manifest['seed_id'] != info['checkpoint'] or manifest['sha256'] != info['save_sha256']
                or world_start.content(manifest) != world_start.content(self.recipe)):
            raise WorldStoreError('checkpoint save identity mismatch')
        origin = save_origin.read(snapshot, manifest['format'])
        if (origin != info['native_origin'] or origin['generation_seed'] != self.recipe['generation_seed']
                or origin['gamestart'] != self.recipe['gamestart']):
            raise WorldStoreError('native save origin differs from the world recipe/checkpoint')
        return dict(info=info, manifest=manifest, folder=folder, snapshot=snapshot,
                    checkpoint_sha256=pointer['sha256'])

    def publish(self, snapshot, *, expected_revision):
        """Commit completed native save bytes with a compare-and-swap revision.

        Existing checkpoints are immutable; a crash before HEAD replacement leaves
        the old head valid. Unreferenced staging/final directories stay as evidence.
        """
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**53-1:
            raise WorldStoreError('invalid expected revision')
        with _lock(self.root):
            before = self.latest()
            revision = before['info']['revision'] if before else 0
            if expected_revision != revision:
                raise WorldStoreError('stale host checkpoint revision')
            checkpoint = secrets.token_hex(16)
            staging = self.root / 'checkpoints' / ('.pending-' + checkpoint)
            manifest = seed.create_bundle(snapshot, staging, seed_id=checkpoint,
                                          **world_start.content(self.recipe))
            origin = save_origin.read(staging / 'seed.bin', manifest['format'])
            if (origin['generation_seed'] != self.recipe['generation_seed']
                    or origin['gamestart'] != self.recipe['gamestart']
                    or (before and origin != before['info']['native_origin'])):
                raise WorldStoreError('native save belongs to another world/start/build')
            # seed.create_bundle fsyncs save bytes; also flush its completed manifest.
            with (staging / 'manifest.json').open('r+b') as stream:
                os.fsync(stream.fileno())
            info = dict(protocol=1, world_id=self.recipe['world_id'], revision=revision+1,
                        checkpoint=checkpoint, parent=before['checkpoint_sha256'] if before else None,
                        recipe_sha256=self.recipe_digest, save_sha256=manifest['sha256'], native_origin=origin)
            _write(staging / 'checkpoint.json', info)
            _sync_dir(staging)
            folder = staging.with_name(checkpoint)
            if folder.exists() or folder.is_symlink():
                raise WorldStoreError('checkpoint ID collision; existing data preserved')
            staging.rename(folder)
            _sync_dir(folder.parent)
            self._unchanged()
            temporary = self.root / ('.HEAD-' + checkpoint + '.tmp')
            _write(temporary, dict(checkpoint=checkpoint,
                                   sha256=hashlib.sha256(seed.canonical(info)).hexdigest()))
            os.replace(temporary, self.root / 'HEAD.json')
            _sync_dir(self.root)
            return self.latest()
