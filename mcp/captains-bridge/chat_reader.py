"""Disposable read-only adapter. Reads one requested chat; stores no transcript."""
import collections
import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from saved_chat import read_saved_chat


def codex_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')


def read_chat(thread_id):
    if not isinstance(thread_id, str) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', thread_id):
        raise ValueError('A valid current-chat identifier is required.')
    return summarize(read_saved_chat(codex_home(), thread_id))


def summarize(thread):
    turns, records = [], []
    for turn in thread.get('turns', []):
        counts, failures, delegations, files = collections.Counter(), [], [], set()
        request, updates, outcome = '', [], ''
        for index, item in enumerate(turn.get('items', [])):
            kind = item.get('type')
            record = None
            if kind == 'userMessage':
                text = '\n'.join(c.get('text', '') for c in item.get('content', []) if c.get('type') == 'text')
                # Runtime context is not a separate request from the Captain.
                if text and not text.startswith(('<environment_context>', '# AGENTS.md', '<in-app-browser-context', '<external_')):
                    request = text
                    record = {'actor': 'Captain', 'kind': 'request', 'text': text}
            elif kind == 'agentMessage':
                text = item.get('text', '')
                if item.get('phase') == 'final_answer':
                    outcome = text
                elif text:
                    updates.append(text)
                if text:
                    record = {'actor': 'First officer', 'kind': 'report' if item.get('phase') == 'final_answer' else 'progress', 'text': text}
            elif kind == 'commandExecution':
                counts['commands'] += 1
                record = {'actor': 'First officer', 'kind': 'command', 'text': str(item.get('command', '')) + '\n' + str(item.get('aggregatedOutput') or ''), 'exitCode': item.get('exitCode')}
                if item.get('exitCode') not in (None, 0):
                    failures.append({'kind': 'command', 'label': 'A command returned exit code ' + str(item['exitCode']), 'status': item.get('status')})
            elif kind == 'webSearch':
                counts['searches'] += 1
            elif kind == 'mcpToolCall':
                counts['toolCalls'] += 1
                result = item.get('result') or {}
                # Exclude the viewer's own calls to avoid recursively copying its payload.
                if not any(name in str(item.get('server', '')) for name in ('captains_bridge', 'observation_spike')):
                    content = result.get('content', []) if isinstance(result, dict) else []
                    result_text = '\n'.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
                    record = {'actor': 'First officer', 'kind': 'tool', 'text': str(item.get('tool', 'Tool call')) + '\n' + result_text}
                if item.get('status') == 'failed' or item.get('error') or (isinstance(result, dict) and result.get('isError')):
                    failures.append({'kind': 'tool', 'label': item.get('tool', 'Tool call'), 'status': item.get('status')})
            elif kind == 'fileChange':
                counts['fileEdits'] += 1
                for change in item.get('changes', []):
                    if change.get('path'):
                        files.add(change['path'])
                record = {'actor': 'First officer', 'kind': 'change', 'text': json.dumps(item.get('changes', []), ensure_ascii=False)}
            elif kind == 'collabAgentToolCall':
                counts['delegationCalls'] += 1
                delegations.append({'action': item.get('tool'), 'status': item.get('status'),
                                    'agents': list(item.get('receiverThreadIds') or []),
                                    'prompt': item.get('prompt') or ''})
                record = {'actor': 'First officer', 'kind': 'handoff', 'text': json.dumps({'action': item.get('tool'), 'status': item.get('status'), 'prompt': item.get('prompt'), 'recipients': item.get('receiverThreadIds')}, ensure_ascii=False)}
            if record and record['text'].strip():
                original = record['text']
                record.update({'id': str(item.get('id') or f"{turn['id']}:{index}"), 'turnId': turn['id'], 'text': original[:16000], 'truncated': len(original) > 16000})
                # Per-operation timestamps are not supplied by this API. Never borrow the turn's start.
                records.append(record)
        turns.append({'id': turn['id'], 'request': request, 'status': ('recordOpen' if turn.get('completedAt') is None and turn.get('status') == 'interrupted' else turn.get('status')),
                      'storedStatus': turn.get('status'), 'startedAt': turn.get('startedAt'), 'completedAt': turn.get('completedAt'),
                      'durationMs': turn.get('durationMs'), 'counts': dict(counts),
                      'failures': failures, 'delegations': delegations, 'files': sorted(files),
                      'updates': updates, 'outcome': outcome})
    return {'title': thread.get('name') or 'This chat',
            'readAt': datetime.now(timezone.utc).isoformat(), 'turns': turns, 'records': records,
            'createdAt': thread.get('createdAt'), 'updatedAt': thread.get('updatedAt'),
            'coverage': 'Codex’s existing saved turns and completed item records: messages, tool outcomes, and explicit agent handoffs. Live unfinished operations and worker-side Hermes events are not connected.'}
