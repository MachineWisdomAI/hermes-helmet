"""Adapter for Codex's saved item-completion events; no subprocess or writes.

This private storage format is version-dependent. Reject unsupported history
instead of guessing a chat or silently producing an empty walkthrough.
"""
import json
import os
import re
import shlex
import sqlite3
from pathlib import Path


def indexed_chat(home, thread_id):
    databases = [p for p in home.glob('state_*.sqlite') if re.fullmatch(r'state_\d+\.sqlite', p.name)]
    if not databases:
        raise ValueError('Codex’s saved chat index was not found.')
    database = max(databases, key=lambda p: int(p.stem.split('_')[-1]))
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only = ON')
            columns = {r[1] for r in connection.execute('PRAGMA table_info(threads)')}
            if not {'id', 'rollout_path'} <= columns:
                raise ValueError('This Codex chat-index format is not supported by Captain’s Bridge.')
            fields = ['id', 'rollout_path'] + [key for key in ('name', 'created_at', 'updated_at') if key in columns]
            row = connection.execute('SELECT ' + ','.join(fields) + ' FROM threads WHERE id = ?', (thread_id,)).fetchone()
            if row is None:
                raise ValueError('This exact chat was not found in Codex’s saved index.')
            return dict(row)
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise ValueError('Codex’s saved chat index could not be read in read-only mode.') from error


# Known record kinds that are intentionally not evidence sources.
EXCLUDED_KINDS = {'Reasoning', 'RawResponse', 'RawResponseItem'}
UNSUPPORTED_ITEM = 'This saved item format is not supported by Captain’s Bridge.'


def _objects(value):
    if not isinstance(value, list) or not all(isinstance(entry, dict) for entry in value):
        raise ValueError(UNSUPPORTED_ITEM)
    return value


def normalize_item(item):
    if not isinstance(item, dict) or not isinstance(item.get('type'), str):
        raise ValueError(UNSUPPORTED_ITEM)
    kind = item['type']
    common = {'id': item.get('id')}
    if kind == 'UserMessage':
        return dict(common, type='userMessage', content=_objects(item.get('content', [])))
    if kind == 'AgentMessage':
        text = '\n'.join(c.get('text', '') for c in _objects(item.get('content', [])) if str(c.get('type', '')).lower() == 'text')
        return dict(common, type='agentMessage', text=text, phase=item.get('phase'))
    if kind == 'CommandExecution':
        command = item.get('command')
        if isinstance(command, list):
            command = shlex.join(command)
        return dict(common, type='commandExecution', command=command,
                    aggregatedOutput=item.get('aggregated_output'), exitCode=item.get('exit_code'), status=item.get('status'))
    if kind == 'McpToolCall':
        return dict(common, type='mcpToolCall', **{key: item.get(key) for key in ('server', 'tool', 'result', 'error', 'status')})
    if kind == 'FileChange':
        changes = item.get('changes', {})
        if not isinstance(changes, dict):
            raise ValueError('This saved file-change format is not supported by Captain’s Bridge.')
        return dict(common, type='fileChange', changes=[dict(change, path=path) for path, change in changes.items()])
    if kind == 'CollabAgentToolCall':
        return dict(common, type='collabAgentToolCall', tool=item.get('tool'), status=item.get('status'),
                    prompt=item.get('prompt'), receiverThreadIds=item.get('receiver_thread_ids', []))
    if kind in EXCLUDED_KINDS:
        return None
    raise _Unsupported(kind)


class _Unsupported(Exception):
    """An item kind this adapter does not know; not counted as usable history."""


def read_saved_chat(home, thread_id):
    home = Path(home).resolve()
    row = indexed_chat(home, thread_id)
    path = Path(row['rollout_path']).resolve()
    if not any(path.is_relative_to(home / directory) for directory in ('sessions', 'archived_sessions')):
        raise ValueError('This chat’s saved record is outside Codex’s session directories.')
    turns, item_count, unsupported, identity = {}, 0, 0, None

    def turn_for(turn_id):
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError('A saved event is missing its turn identity.')
        return turns.setdefault(turn_id, {'id': turn_id, 'status': 'recordOpen', 'items': {},
                                         'startedAt': None, 'completedAt': None, 'durationMs': None})

    with path.open('rb') as stream:
        # Bound the read to bytes present when opened. Ignore an incomplete final
        # append, but never hide malformed complete lines in the saved history.
        remaining = os.fstat(stream.fileno()).st_size
        while remaining > 0:
            line = stream.readline(remaining)
            remaining -= len(line)
            if not line or not line.endswith(b'\n'):
                break
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError) as error:
                raise ValueError('A saved chat record could not be decoded.') from error
            if not isinstance(event, dict):
                raise ValueError('A saved chat record has an unsupported shape.')
            payload = event.get('payload', {})
            if not isinstance(payload, dict):
                raise ValueError('A saved chat record has an unsupported shape.')
            if event.get('type') == 'session_meta':
                identity = payload.get('id')
                if identity != thread_id:
                    raise ValueError('The saved chat identity does not match this chat.')
            if event.get('type') != 'event_msg':
                continue
            if identity != thread_id:
                raise ValueError('The saved chat identity could not be verified.')
            if payload.get('thread_id', thread_id) != thread_id:
                raise ValueError('A saved event belongs to another chat.')
            kind = payload.get('type')
            if kind == 'task_started':
                turn_for(payload.get('turn_id'))['startedAt'] = payload.get('started_at')
            elif kind == 'task_complete':
                turn_for(payload.get('turn_id')).update(status='completed', startedAt=payload.get('started_at'),
                                                       completedAt=payload.get('completed_at'), durationMs=payload.get('duration_ms'))
            elif kind == 'item_completed':
                try:
                    item = normalize_item(payload.get('item'))
                except _Unsupported:
                    unsupported += 1
                    continue
                if item is not None:
                    item_count += 1
                    if not item.get('id'):
                        raise ValueError('A saved source record is missing its identity.')
                    turn_for(payload.get('turn_id'))['items'][item['id']] = item
    if identity != thread_id:
        raise ValueError('The saved chat identity could not be verified.')
    if not item_count and unsupported:
        raise ValueError('This saved chat uses item records that Captain’s Bridge does not support, so it cannot build its walkthrough.')
    if not item_count:
        raise ValueError('This saved chat format has no completed item records yet. Captain’s Bridge cannot build its walkthrough.')
    for turn in turns.values():
        turn['items'] = list(turn['items'].values())
    return {'name': row.get('name'), 'createdAt': row.get('created_at'), 'updatedAt': row.get('updated_at'),
            'turns': list(turns.values())}
