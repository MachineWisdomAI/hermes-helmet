"""Complete, redacted Hermes snapshots with content-addressed redaction reuse.

Runs in a single profile's worker environment. Reads the native SessionDB API;
never copies or changes a session database. Only redacted records reach the
owner-only cache or stdout. Callers must discard stdout on a nonzero exit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import sys
import tempfile
from typing import Callable


class SnapshotError(RuntimeError):
    pass


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class SnapshotCache:
    def __init__(self, root: Path, redact: Callable, identity: str):
        self.root, self.redact, self.identity = root, redact, identity
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise SnapshotError('snapshot cache must be an owner-only regular directory')

    def _path(self, session_id: str) -> Path:
        return self.root / (_digest(session_id.encode()) + '.json')

    def get(self, record: dict) -> dict:
        session_id = record.get('id')
        if not isinstance(session_id, str) or not session_id:
            raise SnapshotError('invalid session identity')
        fingerprint = _digest((self.identity + '\0' + _json(record)).encode())
        path = self._path(session_id)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            fd = None
        except OSError:
            raise SnapshotError('unsafe snapshot cache entry') from None
        if fd is not None:
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) & 0o077):
                    raise SnapshotError('unsafe snapshot cache entry')
                try:
                    cached = json.load(stream)
                except (ValueError, UnicodeError):
                    cached = None
            if (isinstance(cached, dict) and cached.get('fingerprint') == fingerprint
                    and isinstance(cached.get('record'), dict)
                    and cached['record'].get('id') == session_id
                    and isinstance(cached['record'].get('messages'), list)):
                return cached['record']
        redacted = self.redact(record)
        if (not isinstance(redacted, dict) or redacted.get('id') != session_id
                or not isinstance(redacted.get('messages'), list)):
            raise SnapshotError('invalid redacted session')
        fd, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(_json({'fingerprint': fingerprint, 'record': redacted}))
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return redacted

    def prune(self, session_ids: set[str]) -> None:
        keep = {self._path(s).name for s in session_ids}
        for path in self.root.iterdir():
            if re.fullmatch(r'[a-f0-9]{64}\.json', path.name) and path.name not in keep:
                path.unlink()


def redaction_identity() -> str:
    """Invalidate across code, additive plugin patterns and profile vault changes.

    These registries belong to Hermes' native force-mode redactor. Unknown
    runtimes fail closed instead of trusting a cache with an incomplete key.
    Registry values are hashed in memory; they are never logged or persisted.
    """
    import agent.redact as redactor
    import hermes_cli.session_export_md as renderer
    try:
        with redactor._VAULT_REDACTION_LOCK:
            vault = tuple(redactor._VAULT_REDACTION_VALUES.get(redactor._vault_scope(), {}))
        with redactor._registry_lock:
            plugins = redactor._PLUGIN_PREFIX_PATTERNS
            patterns = [(str(k), list(v)) for k, v in sorted(plugins.items())]
        material = b'helmet-session-snapshot-v1\0'
        for module in (redactor, renderer):
            material += Path(module.__file__).read_bytes() + b'\0'
        material += _json([vault, patterns]).encode()
        return _digest(material)
    except (AttributeError, OSError, TypeError):
        raise SnapshotError('runtime redaction identity is unavailable') from None


def export_snapshot(cache_dir: Path, output) -> int:
    from hermes_state import SessionDB, resolved_max_export_messages
    from hermes_cli.session_export_md import redact_session_data
    identity = redaction_identity()
    cache = SnapshotCache(cache_dir, redact_session_data, identity)
    # Spool only redacted data; keep stdout empty until every session succeeds.
    with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as spool:
        with SessionDB(read_only=True) as db:
            sessions = db.search_sessions(source=None, limit=100000)
            if len(sessions) >= 100000:
                raise SnapshotError('snapshot session limit reached')
            limit = resolved_max_export_messages()
            if limit:
                db.assert_exports_safe([s['id'] for s in sessions], max_messages=limit)
            records = db.export_all(source=None, include_compacted=False, include_inactive=True)
            ids = set()
            for record in records:
                spool.write(_json(cache.get(record)) + '\n')
                ids.add(record['id'])
        if redaction_identity() != identity:
            raise SnapshotError('redaction rules changed during snapshot; retry')
        cache.prune(ids)
        spool.seek(0)
        shutil.copyfileobj(spool, output)
    return len(ids)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--timeout', type=float, default=20)
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    from hermes_constants import get_hermes_home
    cache_dir = args.cache_dir or get_hermes_home() / 'cache/helmet-session-snapshots'
    def timed_out(*_):
        raise SnapshotError('snapshot deadline exceeded; completed redacted cache entries retained')
    old_handler = signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, args.timeout)
    try:
        export_snapshot(cache_dir, sys.stdout)
        return 0
    except Exception:
        # Native errors may contain source session content or credentials.
        print('Hermes snapshot failed or exceeded its deadline; no complete snapshot emitted.', file=sys.stderr)
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


if __name__ == '__main__':
    raise SystemExit(main())
