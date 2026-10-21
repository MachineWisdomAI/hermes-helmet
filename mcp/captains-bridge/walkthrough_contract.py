"""The one source of the walkthrough constraints.

`walkthrough.validate` enforces these values and `server.py` publishes them in
`tools/list` through `walkthrough_schema()`, so discovery and runtime cannot
drift. Text limits are Python `len()` of the submitted string (Unicode code
points) measured before trimming; invalid text is rejected, never shortened.
"""
import re

# json.dumps(account) with default ASCII escaping, measured before normalization.
# It is a serialized character count, not a UTF-8 byte budget: each non-ASCII
# character counts as 6 characters (12 outside the Basic Multilingual Plane).
MAX_SERIALIZED_CHARS = 100000

# Maximum text length, keyed by schema path ("[]" is any array index).
LIMITS = {
    'objective': 600,
    'summary': 1000,
    'items[].id': 100,
    'items[].title': 140,
    'items[].summary': 600,
    'items[].detail': 2500,
    'items[].status': 60,
    'items[].steps[].label': 140,
    'items[].steps[].detail': 1000,
    'items[].relations[].label': 140,
    'items[].relations[].detail': 1000,
    'items[].reported.label': 80,
    'items[].reported.actor': 60,
    'items[].disposition.label': 80,
    'items[].disposition.actor': 60,
    'items[].change.before': 120,
    'items[].change.after': 120,
    'items[].change.explanation': 1200,
    'items[].links[].label': 160,
    'items[].links[].url': 2000,
}
# Fields that name something for a human and so reject internal planning IDs.
READABLE_NAMES = frozenset((
    'items[].title', 'items[].steps[].label', 'items[].relations[].label', 'items[].links[].label'))
INTERNAL_ID = re.compile(r'\b(?:t_[0-9a-f]{6,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b', re.I)

EVIDENCE_REFS = (1, 30)  # rejected outside this count (duplicates count, then collapse)
ITEMS = (1, 24)          # rejected outside this count
# Only this many leading entries are read; later entries are ignored, not rejected.
PROCESSED_PREFIX = {'items[].steps': 16, 'items[].relations': 8, 'items[].links': 8}

GROUPS = ('changed', 'unresolved', 'activity')
ACTORS = ('Captain', 'First officer', 'Hermes', 'Other agent')
RELATION_KINDS = ('Handoff', 'Wakeup', 'Follow-up')
OPTIONAL_OBJECTS = ('reported', 'disposition', 'change')  # falsy values are treated as absent
URL = r'^https://[^\s<>]+$'  # and the URL must occur in a cited record


def template(path):
    """Schema key of a concrete path such as items[3].steps[0].label."""
    return re.sub(r'\[\d+\]', '[]', path)


def _text(key, note=''):
    description = f'Text, 1 to {LIMITS[key]} characters measured before trimming; not blank. Never shortened for you.'
    if key in READABLE_NAMES:
        description += ' A readable name: planning IDs (t_… tasks) and chat UUIDs are rejected.'
    return {'type': 'string', 'minLength': 1, 'maxLength': LIMITS[key], 'description': description + note}


def _evidence():
    low, high = EVIDENCE_REFS
    return {'type': 'array', 'minItems': low, 'maxItems': high, 'items': {'type': 'string'},
            'description': f'{low} to {high} record IDs returned by read_chat_work for this bound chat. '
                           'An ID not in this chat is rejected; duplicates are collapsed.'}


def _prefixed(key, schema, what):
    count = PROCESSED_PREFIX[key]
    return {'type': 'array', 'items': schema, 'x-processedPrefix': count,
            'description': f'Optional. Only the first {count} {what} are read; later entries are ignored, not rejected.'}


def _object(properties, required, description):
    return {'type': 'object', 'properties': properties, 'required': list(required),
            'description': (description + ' Extra properties are ignored.').strip()}


def walkthrough_schema():
    """JSON Schema for the `walkthrough` argument of the accepting tools."""
    steps = _object({'actor': {'type': 'string', 'enum': list(ACTORS), 'description': 'Hermes needs a cited record that mentions Hermes.'},
                     'label': _text('items[].steps[].label'), 'detail': _text('items[].steps[].detail'),
                     'evidence': _evidence()}, ('actor', 'label', 'detail', 'evidence'), 'A chronological step.')
    relations = _object({'kind': {'type': 'string', 'enum': list(RELATION_KINDS)},
                         'label': _text('items[].relations[].label'), 'detail': _text('items[].relations[].detail'),
                         'evidence': _evidence()}, ('kind', 'label', 'detail', 'evidence'),
                        'An explicitly recorded asynchronous relationship.')
    links = _object({'label': _text('items[].links[].label'),
                     'url': {**_text('items[].links[].url'), 'pattern': URL,
                             'description': f'HTTPS URL, 1 to {LIMITS["items[].links[].url"]} characters, without '
                                            'whitespace or < >, that occurs in a record cited by this link\'s evidence.'},
                     'evidence': _evidence()}, ('label', 'url', 'evidence'), 'A source link.')

    def paired(name):
        return _object({'label': _text(f'items[].{name}.label'), 'actor': _text(f'items[].{name}.actor'),
                        'evidence': _evidence()}, ('label', 'actor', 'evidence'),
                       'Optional. Treated as absent when empty or null.')
    change = _object({'before': _text('items[].change.before'), 'after': _text('items[].change.after'),
                      'explanation': _text('items[].change.explanation'), 'evidence': _evidence()},
                     ('before', 'after', 'explanation', 'evidence'), 'Optional. Treated as absent when empty or null.')
    item = _object({
        'id': _text('items[].id', ' Unique among items.'),
        'group': {'type': 'string', 'enum': list(GROUPS)},
        'title': _text('items[].title'), 'summary': _text('items[].summary'),
        'detail': _text('items[].detail'), 'status': _text('items[].status'),
        'evidence': _evidence(),
        'steps': _prefixed('items[].steps', steps, 'steps'),
        'relations': _prefixed('items[].relations', relations, 'relations'),
        'reported': paired('reported'), 'disposition': paired('disposition'), 'change': change,
        'links': _prefixed('items[].links', links, 'links'),
    }, ('id', 'group', 'title', 'summary', 'detail', 'status', 'evidence'), 'One piece of work.')
    low, high = ITEMS
    return _object({
        'objective': _text('objective'), 'summary': _text('summary'), 'evidence': _evidence(),
        'items': {'type': 'array', 'minItems': low, 'maxItems': high, 'items': item,
                  'description': f'{low} to {high} work items.'},
    }, ('objective', 'summary', 'evidence', 'items'),
        f'Source-grounded explanation. Before normalization, len(json.dumps(walkthrough)) must be at most '
        f'{MAX_SERIALIZED_CHARS}: a serialized character count with default ASCII escaping, not a UTF-8 byte '
        'budget, so non-ASCII text counts for more than its length. See mcp/captains-bridge/example-walkthrough.json.')
