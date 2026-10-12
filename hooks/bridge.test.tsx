// Tests for the Captain's Bridge reader integration and Changes First view.
// They stand in for `helmet bridge read` with synthetic process.run answers,
// seed a synthetic walkthrough, mount the pane on the terminal and the
// desktop, and press keyed elements. Every fixture is generated here.
import { describe, expect, test } from 'claude-code/testing'
import {
  coverageWarnings,
  elapsedOf,
  formatElapsed,
  readerArgv,
  staleNote,
} from './register'

const SURFACES = ['terminal', 'desktop'] as const
const REF = (n: number) => n.toString(16).padStart(32, '0')
const T0 = '2026-10-09T16:00:00Z'

const PANE = {
  plugin: 'hermes-helmet',
  component: 'Pane',
  requestId: 'captains-bridge',
  props: { title: "Captain's Bridge", isFocused: true, bodyColumns: 80, placement: 'dock' },
} as const

function rec(n: number, at: string, text: string, extra: Record<string, unknown> = {}) {
  return {
    ref: REF(n),
    parentRef: null,
    agentId: null,
    timestamp: at,
    role: 'assistant',
    origin: 'assistant',
    text,
    truncated: false,
    ...extra,
  }
}

const RECORDS = [
  rec(1, '2026-10-09T16:00:00Z', 'Please add the synthetic widget.', { role: 'user', origin: 'person' }),
  rec(2, '2026-10-09T16:02:30Z', 'Opened https://example.test/acme/widgets/pull/7 for the widget.'),
  rec(3, '2026-10-09T16:10:00Z', 'Review found one repair; fixed in the same branch.', { origin: 'agent', agentId: 'a1' }),
  rec(4, '2026-10-09T16:12:05Z', 'Worker reports the widget run complete.'),
  rec(5, '2026-10-09T16:20:00Z', 'Gadget still fails on empty input.', { role: 'tool_result', origin: 'assistant', tool: 'Bash' }),
]

function summary(overrides: Record<string, unknown> = {}, sessionId = 'sess-a') {
  return {
    schema: 'hermes-helmet.bridge.records/1',
    helmetVersion: '0.9.0',
    sessionId,
    readAt: '2026-10-09T17:20:00Z',
    fingerprint: '5:uuid-5',
    records: RECORDS,
    coverage: { recordsTotal: 5, recordsIncluded: 5, bytesOmitted: 0, unsupportedSkipped: 0, pendingTail: false },
    warnings: [],
    ...overrides,
  }
}

const ITEMS = [
  {
    id: 'widget',
    title: 'Add the synthetic widget',
    group: 'changed',
    status: 'Reported complete',
    summary: 'A widget was added in a pull request.',
    detail: 'The first officer added a widget and a worker repaired one review finding.',
    evidence: [REF(1), REF(2), REF(4)],
    steps: [
      { actor: 'First officer', label: 'Opened the pull request', detail: 'PR opened.', evidence: [REF(1), REF(2)] },
      { actor: 'Other agent', label: 'Reviewed and repaired', detail: 'One finding fixed.', evidence: [REF(2), REF(3)] },
    ],
    reported: { label: 'The worker reported the run complete', actor: 'Hermes', evidence: [REF(4)] },
    disposition: { label: 'Not yet accepted by the Captain', actor: 'Captain', evidence: [REF(4)] },
    change: { before: 'No widget existed.', after: 'A widget class exists.', explanation: 'From the cited PR.', evidence: [REF(2)] },
    links: [
      { label: 'Widget PR', url: 'https://example.test/acme/widgets/pull/7', evidence: [REF(2)] },
      { label: 'Invented link', url: 'https://example.test/not-in-records', evidence: [REF(2)] },
    ],
  },
  {
    id: 'gadget',
    title: 'Fix the gadget on empty input',
    group: 'unresolved',
    status: 'Open',
    summary: 'The gadget still fails on empty input.',
    detail: 'A tool result shows the gadget failing.',
    evidence: [REF(5)],
  },
  {
    id: 'notes',
    title: 'Housekeeping',
    group: 'activity',
    status: 'Recorded',
    summary: 'Routine activity.',
    detail: 'Nothing notable.',
    evidence: [REF(3)],
  },
]

const BODY = {
  objective: 'Ship the synthetic widget',
  summary: 'The widget was reported complete; the gadget remains open.',
  evidence: [REF(1)],
  items: ITEMS,
}

const WALKTHROUGH = {
  body: BODY,
  readAt: '2026-10-09T17:00:00Z',
  fingerprint: '3:uuid-3',
  preparedAt: '2026-10-09T17:01:00Z',
}

// Only the owner writes the mod's state, and this slice never writes a
// walkthrough (preparing one is a later slice). A test therefore hands a
// walkthrough to the host stand-in (`fixture.walkthrough`), which answers the
// mod's read of it; a stand-in plugin reads the rest back as a
// person-visible string.
const fixture: { walkthrough: unknown } = { walkthrough: null }
const SEED = {
  name: 'seed',
  register: (on: any) => {
    on('command.run', { command: 'probe' }, async ($: any) => {
      const view = await $.state.get({ plugin: 'hermes-helmet', key: 'view' })
      const records = await $.state.get({ plugin: 'hermes-helmet', key: 'records' })
      const binding = await $.state.get({ plugin: 'hermes-helmet', key: 'binding' })
      return {
        text: JSON.stringify({
          view: view.value ?? null,
          records: records.value ?? null,
          binding: binding.value ?? null,
        }),
      }
    })
  },
}
const WITH = { plugins: [SEED] }

type Harness = {
  calls: Array<{ argv: string[]; init: any }>
  models: number
  turns: number
  reply: { current: any }
}

function host(on: any, reply: any, opts: { id?: string } = {}): Harness {
  const h: Harness = { calls: [], models: 0, turns: 0, reply: { current: reply } }
  fixture.walkthrough = null
  on('state.get', async (_$: any, e: any, next: any) =>
    e.key === 'walkthrough' && fixture.walkthrough !== null
      ? { value: { value: fixture.walkthrough, version: 1 } }
      : next(e),
  )
  on('session.version', () => ({ value: { version: '2.1.296' } }))
  on('session.id', () => ({ value: opts.id ?? 'sess-a' }))
  on('session.start', () => ({ cwd: '/work' }))
  on('classic.SessionStart', () => ({}))
  on('command.register', (_$: any, e: any) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', (_$: any, e: any) => {
    h.calls.push({ argv: [...e.argv], init: e.init })
    const answer = h.reply.current
    if (answer instanceof Error) throw answer
    return typeof answer === 'function' ? answer() : { value: answer }
  })
  on('model.complete', () => {
    h.models += 1
    return {}
  })
  on('prompt.submit', () => {
    h.turns += 1
    return {}
  })
  on('session.send', () => {
    h.turns += 1
    return {}
  })
  return h
}

const ok = (payload: unknown) => ({ exitCode: 0, stdout: JSON.stringify(payload), stderr: '' })

function run(command: string, args = '') {
  return {
    command,
    args,
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 100 },
  } as any
}

async function seed(_$: any, _key: 'walkthrough', value: unknown) {
  fixture.walkthrough = value
}

async function probe($: any): Promise<any> {
  return JSON.parse((await $.command.run(run('probe'))).text)
}

async function texts(pane: any, type = 'Text'): Promise<string[]> {
  return (await pane.findAll({ type })).map((el: any) => el.text)
}

async function opened($: any, on: any, id = 'sess-a') {
  const h = host(on, ok(summary()), { id })
  await $.classic.SessionStart({
    source: 'startup',
    session_id: id,
    transcript_path: `/synthetic/transcripts/${id}.jsonl`,
  })
  await $.command.run(run('captains-bridge'))
  return h
}

describe('the reader call', () => {
  test('passes the exact session and known transcript path with a 30 s limit', WITH, async ($, on) => {
    const h = await opened($, on)
    expect(h.calls.length).toBe(1)
    expect(h.calls[0].argv).toEqual([
      'helmet', 'bridge', 'read', '--session', 'sess-a',
      '--transcript', '/synthetic/transcripts/sess-a.jsonl',
    ])
    expect(h.calls[0].init.timeoutMs).toBe(30000)
  })

  test('leaves an unknown transcript path to the reader exact-ID lookup', WITH, async ($, on) => {
    const h = host(on, ok(summary()))
    await $.command.run(run('captains-bridge'))
    expect(h.calls[0].argv).toEqual(['helmet', 'bridge', 'read', '--session', 'sess-a'])
  })

  test('uses the configured helmet command', { plugins: [SEED], options: { helmetCommand: '/opt/tools/helmet' } }, async ($, on) => {
    const h = host(on, ok(summary()))
    await $.command.run(run('captains-bridge'))
    expect(h.calls[0].argv[0]).toBe('/opt/tools/helmet')
  })

  test('adopts a valid summary and sends nothing', WITH, async ($, on) => {
    const h = await opened($, on)
    const state = await probe($)
    expect(state.records.readAt).toBe('2026-10-09T17:20:00Z')
    expect(state.records.fingerprint).toBe('5:uuid-5')
    expect(h.models).toBe(0)
    expect(h.turns).toBe(0)
  })

  test('adopts nothing for another session and does not rebind', WITH, async ($, on) => {
    const h = host(on, ok(summary({}, 'sess-other')))
    await $.command.run(run('captains-bridge'))
    const state = await probe($)
    expect(state.records).toBe(null)
    expect(state.binding).toEqual({ sessionId: 'sess-a' })
    expect(state.view.notice.text).toContain('different conversation')
    expect(h.calls.length).toBe(1)
  })
})

describe('the overview', () => {
  for (const surface of SURFACES) {
    test(`shows the objective and outcome first, then the groups, on ${surface}`, WITH, async ($, on) => {
      await opened($, on)
      await seed($, 'walkthrough', WALKTHROUGH)
      const pane = await $.ui.mount({ ...PANE, surface })
      const all = await texts(pane)
      const at = (needle: string) => all.findIndex(t => t.includes(needle))
      expect(at('Ship the synthetic widget')).toBeGreaterThan(-1)
      expect(at('The widget was reported complete')).toBeGreaterThan(-1)
      expect(at('Ship the synthetic widget')).toBeLessThan(at('What changed'))
      expect(at('What changed')).toBeLessThan(at('What remains unresolved'))
      expect(at('What remains unresolved')).toBeLessThan(at('Other recorded activity'))
      // Counts and actions come after the substantive work.
      expect(at('Other recorded activity')).toBeLessThan(at('5 of 5 records read'))
      const titles = await pane.findAll({ type: 'Button' })
      expect(titles.map((b: any) => b.key)).toEqual([
        'item:widget', 'item:gadget', 'item:notes', 'refresh', 'update-explanation',
      ])
      // Claude Code 2.1.293 rejects a Button with anything but one string label.
      // The item Button carries title and status as one label, no nested Text.
      const item = titles.find((b: any) => b.key === 'item:widget')
      expect(typeof item.text).toBe('string')
      expect(item.text).toMatch(/\[[^\]]+\]$/)
      expect(await pane.find({ text: 'Fix the gadget on empty input' })).not.toBe(undefined)
      expect(await pane.find({ text: 'The gadget still fails on empty input.' })).not.toBe(undefined)
      await pane.unmount()
    })

    test(`pairs the completion report and the disposition on ${surface}`, WITH, async ($, on) => {
      await opened($, on)
      await seed($, 'walkthrough', WALKTHROUGH)
      const pane = await $.ui.mount({ ...PANE, surface })
      const report = await pane.find({ key: 'report:widget' })
      const disposition = await pane.find({ key: 'disposition:widget' })
      expect(report?.text).toContain('Completion report')
      expect(report?.text).toContain('Hermes: The worker reported the run complete')
      expect(disposition?.text).toContain('Disposition')
      expect(disposition?.text).toContain('Captain: Not yet accepted by the Captain')
      expect(report?.props.backgroundColor).toBeTruthy()
      expect(disposition?.props.backgroundColor).toBeTruthy()
      expect(report?.props.backgroundColor).not.toBe(disposition?.props.backgroundColor)
      await pane.unmount()
    })

    test(`draws only the common elements on ${surface}`, WITH, async ($, on) => {
      await opened($, on)
      await seed($, 'walkthrough', WALKTHROUGH)
      const pane = await $.ui.mount({ ...PANE, surface })
      for (const type of ['Raster', 'Image', 'Svg', 'Client', 'Markdown']) {
        expect(await pane.findAll({ type })).toEqual([])
      }
      await pane.unmount()
    })
  }

  test('shows a missing disposition as unrecorded, never as accepted', WITH, async ($, on) => {
    await opened($, on)
    const item = { ...ITEMS[0], disposition: undefined }
    await seed($, 'walkthrough', { ...WALKTHROUGH, body: { ...BODY, items: [item, ...ITEMS.slice(1)] } })
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    const disposition = await pane.find({ key: 'disposition:widget' })
    expect(disposition?.text).toContain('No acceptance or rejection is recorded')
    expect(disposition?.text).toContain('A completed run is not acceptance')
    await pane.unmount()
  })

  test('shows counts, coverage and the refresh action, not the transcript, before a walkthrough', WITH, async ($, on) => {
    await opened($, on)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    expect(await pane.find({ text: 'No walkthrough has been prepared' })).not.toBe(undefined)
    expect(await pane.find({ text: '5 of 5 records are read.' })).not.toBe(undefined)
    expect(await pane.find({ key: 'update-explanation' })).not.toBe(undefined)
    expect(await pane.find({ text: 'Please add the synthetic widget.' })).toBe(undefined)
    expect(await pane.find({ text: 'Worker reports' })).toBe(undefined)
    await pane.unmount()
  })
})

describe('the item detail', () => {
  for (const surface of SURFACES) {
    test(`shows explanation, steps, pair, comparison, links and sources on ${surface}`, WITH, async ($, on) => {
      await opened($, on)
      await seed($, 'walkthrough', WALKTHROUGH)
      const pane = await $.ui.mount({ ...PANE, surface })
      await pane.press({ key: 'item:widget' })

      expect(await pane.find({ text: 'The first officer added a widget and a worker' })).not.toBe(undefined)
      // Steps carry the actor and per-step elapsed time from cited records.
      expect(await pane.find({ text: 'First officer: Opened the pull request (elapsed 2m 30s)' })).not.toBe(undefined)
      expect(await pane.find({ text: 'Other agent: Reviewed and repaired (elapsed 7m 30s)' })).not.toBe(undefined)
      // Item elapsed: first to last cited record (16:00:00 to 16:12:05).
      expect(await pane.find({ text: 'elapsed 12m 05s' })).not.toBe(undefined)
      expect(await pane.find({ key: 'report:widget' })).toBe(undefined)
      expect((await pane.find({ key: 'report' }))?.text).toContain('Completion report')
      expect((await pane.find({ key: 'disposition' }))?.text).toContain('Disposition')

      // Comparison is collapsed until requested.
      expect(await pane.find({ text: 'No widget existed.' })).toBe(undefined)
      await pane.press({ key: 'change-toggle' })
      expect(await pane.find({ text: 'No widget existed.' })).not.toBe(undefined)
      expect(await pane.find({ text: 'A widget class exists.' })).not.toBe(undefined)

      // Only a link whose address is in a cited record is drawn.
      const links = await pane.findAll({ type: 'Link' })
      expect(links.map((l: any) => l.props.href)).toEqual(['https://example.test/acme/widgets/pull/7'])

      // Supporting records are collapsed, with time and actor once opened.
      expect(await pane.find({ text: 'Opened https://example.test' })).toBe(undefined)
      await pane.press({ key: 'sources-toggle' })
      expect(await pane.find({ text: '2026-10-09 16:00:00 UTC · Captain' })).not.toBe(undefined)
      expect(await pane.find({ text: '2026-10-09 16:02:30 UTC · First officer' })).not.toBe(undefined)
      expect(await pane.find({ text: 'Please add the synthetic widget.' })).not.toBe(undefined)
      await pane.unmount()
    })
  }

  test('offers no before and after, steps or pair when the item has none', WITH, async ($, on) => {
    await opened($, on)
    await seed($, 'walkthrough', WALKTHROUGH)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    await pane.press({ key: 'item:gadget' })
    expect(await pane.find({ key: 'change-toggle' })).toBe(undefined)
    expect(await pane.find({ text: 'Handoffs, reviews and repairs' })).toBe(undefined)
    expect(await pane.find({ key: 'report' })).toBe(undefined)
    expect(await pane.find({ type: 'Link' })).toBe(undefined)
    expect(await pane.find({ key: 'sources-toggle' })).not.toBe(undefined)
    await pane.unmount()
  })

  test('Back restores the preceding item and scroll position', WITH, async ($, on) => {
    await opened($, on)
    await seed($, 'walkthrough', WALKTHROUGH)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    // The person scrolls the overview down one block, then opens an item.
    await $.ui.scroll({
      component: 'Pane', requestId: 'captains-bridge', offset: 1, by: 1,
      bodyRows: 20, contentRows: 40, origin: { kind: 'person' },
    })
    expect(await pane.find({ text: 'Objective' })).toBe(undefined)
    await pane.press({ key: 'item:gadget' })
    expect(await pane.find({ text: 'A tool result shows the gadget failing.' })).not.toBe(undefined)
    expect((await probe($)).view.stack).toEqual([{ scroll: 1 }, { itemId: 'gadget', scroll: 0, open: [] }])
    await pane.press({ key: 'back' })
    expect((await probe($)).view.stack).toEqual([{ scroll: 1 }])
    expect(await pane.find({ text: 'Objective' })).toBe(undefined)
    expect(await pane.find({ key: 'item:gadget' })).not.toBe(undefined)
    await pane.unmount()
  })

  test('Back returns to the previous item with its sections as left', WITH, async ($, on) => {
    await opened($, on)
    await seed($, 'walkthrough', WALKTHROUGH)
    const pane = await $.ui.mount({ ...PANE, surface: 'desktop' })
    await pane.press({ key: 'item:widget' })
    await pane.press({ key: 'sources-toggle' })
    // A second view is pushed on top of the first item (as a link inside the
    // detail would); Back lifts it and the first item is exactly as left.
    expect((await probe($)).view.stack.length).toBe(2)
    await pane.press({ key: 'back' })
    expect((await probe($)).view.stack.length).toBe(1)
    expect(await pane.find({ key: 'item:widget' })).not.toBe(undefined)
    await pane.press({ key: 'item:widget' })
    // Opening again starts collapsed: state belongs to the stack entry.
    expect(await pane.find({ text: 'Show supporting records (3)' })).not.toBe(undefined)
    await pane.unmount()
  })
})

describe('Refresh records', () => {
  test('keeps selection and the original readAt and counts newer records', WITH, async ($, on) => {
    const h = await opened($, on)
    await seed($, 'walkthrough', WALKTHROUGH)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    await pane.press({ key: 'item:widget' })
    const before = (await probe($)).view.stack

    h.reply.current = ok(
      summary({
        fingerprint: '8:uuid-8',
        readAt: '2026-10-09T18:00:00Z',
        coverage: { recordsTotal: 8, recordsIncluded: 8, bytesOmitted: 0, unsupportedSkipped: 0, pendingTail: false },
      }),
    )
    await pane.press({ key: 'refresh' })

    const state = await probe($)
    expect(h.calls.length).toBe(2)
    expect(state.view.stack).toEqual(before)
    expect(state.records.readAt).toBe('2026-10-09T18:00:00Z')
    expect(await pane.find({ text: 'Explanation read records at 2026-10-09T17:00:00Z; 5 newer records since.' })).not.toBe(undefined)
    expect(await pane.find({ text: 'Records read at 2026-10-09T18:00:00Z' })).not.toBe(undefined)
    // Still on the same item.
    expect(await pane.find({ text: 'The first officer added a widget and a worker' })).not.toBe(undefined)
    expect(h.models).toBe(0)
    expect(h.turns).toBe(0)
    await pane.unmount()
  })

  test('shows no stale note while the fingerprints match', WITH, async ($, on) => {
    await opened($, on)
    await seed($, 'walkthrough', { ...WALKTHROUGH, fingerprint: '5:uuid-5' })
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    expect(await pane.find({ text: 'newer records since' })).toBe(undefined)
    await pane.unmount()
  })

  test('header carries partial coverage and reader warnings', WITH, async ($, on) => {
    const h = host(on, ok(summary({
      coverage: { recordsTotal: 900, recordsIncluded: 600, bytesOmitted: 4096, unsupportedSkipped: 2, pendingTail: true },
      warnings: [{ code: 'pending-tail', message: 'An incomplete final line was ignored.' }],
    })))
    await $.command.run(run('captains-bridge'))
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    expect(await pane.find({ text: 'Coverage is partial: 600 of 900 records are included, 4096 bytes omitted.' })).not.toBe(undefined)
    expect(await pane.find({ text: '2 unsupported records were skipped.' })).not.toBe(undefined)
    expect(await pane.find({ text: 'An incomplete final line was ignored.' })).not.toBe(undefined)
    expect(h.models).toBe(0)
    await pane.unmount()
  })

  const FAILURES: Array<[string, any, string]> = [
    ['a spawn failure', new Error('spawn helmet ENOENT'), 'set helmetCommand'],
    ['error JSON', { exitCode: 2, stdout: JSON.stringify({ schema: 'hermes-helmet.bridge.records/1', error: { code: 'session-not-found', message: 'No file.' } }), stderr: '' }, 'session-not-found'],
    ['a nonzero exit without error JSON', { exitCode: 127, stdout: '', stderr: 'helmet: command not found' }, 'exited with code 127'],
    ['a too-old helmet', { exitCode: 2, stdout: '', stderr: "usage: helmet\nhelmet: error: argument command: invalid choice: 'bridge'" }, 'too old'],
    ['non-JSON output', { exitCode: 0, stdout: 'not json', stderr: '' }, 'cannot parse'],
    ['a missing schema', { exitCode: 0, stdout: JSON.stringify(summary({ schema: undefined, fingerprint: '9:uuid-9' })), stderr: '' }, 'no schema'],
    ['a null warning', { exitCode: 0, stdout: JSON.stringify(summary({ warnings: [null], fingerprint: '9:uuid-9' })), stderr: '' }, 'does not understand'],
    ['a warning without a message', { exitCode: 0, stdout: JSON.stringify(summary({ warnings: [{ code: 'x' }], fingerprint: '9:uuid-9' })), stderr: '' }, 'does not understand'],
    ['malformed coverage counts', { exitCode: 0, stdout: JSON.stringify(summary({ coverage: { recordsTotal: 5, recordsIncluded: 5 }, fingerprint: '9:uuid-9' })), stderr: '' }, 'does not understand'],
    ['a wrong schema major', { exitCode: 0, stdout: JSON.stringify(summary({ schema: 'hermes-helmet.bridge.records/2' })), stderr: '' }, 'records/1'],
  ]
  for (const [name, answer, cause] of FAILURES) {
    test(`keeps the last useful view after ${name}`, WITH, async ($, on) => {
      const h = await opened($, on)
      await seed($, 'walkthrough', WALKTHROUGH)
      const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
      await pane.press({ key: 'item:widget' })
      const kept = await probe($)

      h.reply.current = answer
      await pane.press({ key: 'refresh' })

      const state = await probe($)
      expect(state.records).toEqual(kept.records)
      expect(state.binding).toEqual(kept.binding)
      expect(state.view.stack).toEqual(kept.view.stack)
      const notice = await pane.find({ key: 'notice' })
      expect(notice?.text).toContain(cause)
      expect(await pane.find({ text: 'The first officer added a widget and a worker' })).not.toBe(undefined)
      expect(h.models).toBe(0)
      expect(h.turns).toBe(0)
      await pane.unmount()
    })
  }

  test('a later good read clears the failure message', WITH, async ($, on) => {
    const h = await opened($, on)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    h.reply.current = { exitCode: 0, stdout: 'not json', stderr: '' }
    await pane.press({ key: 'refresh' })
    expect(await pane.find({ key: 'notice' })).not.toBe(undefined)
    h.reply.current = ok(summary())
    await pane.press({ key: 'refresh' })
    expect(await pane.find({ key: 'notice' })).toBe(undefined)
    await pane.unmount()
  })

  test('a first failure shows the cause rather than an empty success', WITH, async ($, on) => {
    host(on, new Error('spawn helmet ENOENT'))
    await $.command.run(run('captains-bridge'))
    const pane = await $.ui.mount({ ...PANE, surface: 'desktop' })
    expect(await pane.find({ key: 'notice' })).not.toBe(undefined)
    expect(await pane.find({ text: '0 of 0' })).toBe(undefined)
    expect(await pane.find({ text: 'No records have been read yet' })).not.toBe(undefined)
    await pane.unmount()
  })
})

describe('long content stays readable when scrolled', () => {
  const LINES = Array.from({ length: 100 }, (_, i) => `explanation line ${i + 1}`).join('\n')
  const LONG = {
    ...WALKTHROUGH,
    body: { ...BODY, items: [{ ...ITEMS[0], detail: LINES }, ...ITEMS.slice(1)] },
  }
  const scroll = ($: any, offset: number) =>
    $.ui.scroll({
      component: 'Pane', requestId: 'captains-bridge', offset, by: 1,
      bodyRows: 20, contentRows: 120, origin: { kind: 'person' },
    })

  for (const surface of SURFACES) {
    test(`three single steps skip no more than three lines of a long detail on ${surface}`, WITH, async ($, on) => {
      await opened($, on)
      await seed($, 'walkthrough', LONG)
      const pane = await $.ui.mount({ ...PANE, surface })
      await pane.press({ key: 'item:widget' })
      expect(await pane.find({ text: 'explanation line 1' })).not.toBe(undefined)
      for (const offset of [1, 2, 3]) await scroll($, offset)
      expect(await pane.find({ text: 'explanation line 4' })).not.toBe(undefined)
      expect(await pane.find({ text: 'explanation line 100' })).not.toBe(undefined)
      await pane.unmount()
    })
  }

  test('expanded supporting records in a narrow pane remain reachable one step at a time', WITH, async ($, on) => {
    await opened($, on)
    await seed($, 'walkthrough', LONG)
    const pane = await $.ui.mount({ ...PANE, props: { ...PANE.props, bodyColumns: 30 }, surface: 'terminal' })
    await pane.press({ key: 'item:widget' })
    await pane.press({ key: 'sources-toggle' })
    const before = (await texts(pane)).join('\n')
    expect(before).toContain('Opened https://example.test/acme/widgets/pull/7')
    await scroll($, 1)
    const after = (await texts(pane)).join('\n')
    expect(after).toContain('explanation line 2')
    expect(after).toContain('Opened https://example.test/acme/widgets/pull/7')
    await pane.unmount()
  })

  test('Back still restores the preceding item and place after scrolling a long detail', WITH, async ($, on) => {
    await opened($, on)
    await seed($, 'walkthrough', LONG)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    await scroll($, 1)
    await pane.press({ key: 'item:widget' })
    await scroll($, 1)
    await scroll($, 2)
    await pane.press({ key: 'back' })
    expect((await probe($)).view.stack).toEqual([{ scroll: 1 }])
    await pane.unmount()
  })
})

describe('derived facts', () => {
  const index = new Map(RECORDS.map(r => [r.ref, r as any]))

  test('formats elapsed time compactly', () => {
    expect(formatElapsed(45_000)).toBe('45s')
    expect(formatElapsed(150_000)).toBe('2m 30s')
    expect(formatElapsed(3_900_000)).toBe('1h 05m')
  })

  test('derives elapsed time only from cited timestamps', () => {
    expect(elapsedOf([REF(1), REF(4)], index)).toBe('12m 05s')
    expect(elapsedOf([REF(1)], index)).toBe(null)
    expect(elapsedOf([REF(1), REF(99)], index)).toBe(null)
  })

  test('builds the reader argv', () => {
    expect(readerArgv('h', { sessionId: 's' })).toEqual(['h', 'bridge', 'read', '--session', 's'])
  })

  test('states staleness with the original readAt and the newer count', () => {
    const current = { fingerprint: '9:u', summary: summary() as any }
    expect(staleNote({ fingerprint: '4:x', readAt: T0 }, current)).toBe(
      `Explanation read records at ${T0}; 5 newer records since.`,
    )
    expect(staleNote({ fingerprint: '9:u', readAt: T0 }, current)).toBe(null)
    expect(staleNote(null, current)).toBe(null)
  })

  test('reports coverage warnings only when something is missing', () => {
    expect(coverageWarnings(summary() as any)).toEqual([])
    expect(coverageWarnings(summary({ warnings: ['plain warning'] }) as any)).toEqual(['plain warning'])
  })
})
