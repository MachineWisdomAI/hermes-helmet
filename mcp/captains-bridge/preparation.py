"""Background walkthrough preparation: portable request identity and settlement.

The server keeps no request state. The panel owns the single active request and
discards any settlement that is not for it, so cancellation, supersession, timeout
and late completion need no cross-process coordination. Nothing is written.
"""
import copy
import re
import secrets
from walkthrough import fingerprint, validate

OUTCOMES = ('delivered', 'failed', 'cancelled', 'superseded')
CHAT = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def new_request(thread_id, data):
    """Capture the originating chat and an immutable snapshot of what was read."""
    return {'requestId': secrets.token_urlsafe(18), 'threadId': thread_id,
            'snapshot': {'fingerprint': fingerprint(data), 'readAt': data['readAt'],
                         'recordCount': len(data['records'])}}


def check_request(request):
    """Return a normalized copy of an untrusted request, or raise ValueError."""
    if not isinstance(request, dict):
        raise ValueError('The walkthrough request is missing.')
    snapshot = request.get('snapshot')
    request_id, thread_id = request.get('requestId'), request.get('threadId')
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}', request_id):
        raise ValueError('The walkthrough request has no valid identity.')
    if not isinstance(thread_id, str) or not CHAT.fullmatch(thread_id):
        raise ValueError('The walkthrough request has no valid originating chat.')
    if not isinstance(snapshot, dict):
        raise ValueError('The walkthrough request has no preparation snapshot.')
    digest, read_at, count = snapshot.get('fingerprint'), snapshot.get('readAt'), snapshot.get('recordCount')
    if (not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
            or not isinstance(read_at, str) or not 0 < len(read_at) <= 80
            or not isinstance(count, int) or isinstance(count, bool) or count < 1):
        raise ValueError('The walkthrough request snapshot is invalid.')
    return {'requestId': request_id, 'threadId': thread_id,
            'snapshot': {'fingerprint': digest, 'readAt': read_at, 'recordCount': count}}


def prepared_walkthrough(request, walkthrough, data):
    """Validate a delivered explanation against its snapshot, not the current read.

    Citations must belong to the records that existed at snapshot time. The result
    keeps the snapshot's fingerprint and read time, so records read later flag it
    as older instead of presenting it as freshly interpreted.
    """
    snapshot = request['snapshot']
    if len(data['records']) < snapshot['recordCount']:
        raise ValueError('The chat has fewer records than the preparation snapshot. Request a new update.')
    bounded = dict(data, records=data['records'][:snapshot['recordCount']])
    checked = validate(copy.deepcopy(walkthrough), bounded)
    checked.update(fingerprint=snapshot['fingerprint'], explainedAt=snapshot['readAt'])
    return checked


def settlement(request, outcome, message=None):
    if outcome not in OUTCOMES:
        raise ValueError('Unknown settlement outcome.')
    value = {'requestId': request['requestId'], 'outcome': outcome}
    if message is not None:
        if not isinstance(message, str) or len(message) > 500:
            raise ValueError('A settlement message must be text, at most 500 characters.')
        value['message'] = message
    return value
