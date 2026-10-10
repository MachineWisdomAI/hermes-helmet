// Tests for preparing and cancelling a source-bound explanation. They stand in
// for `helmet bridge read` and `$.model.complete` (including a synthetic delayed
// reply on the mocked clock), mount the pane and press its keyed buttons.
// Every fixture is generated here; no real session record is used.
import { describe, expect, test, mock } from 'claude-code/testing'
import {
  DIFFICULT_MESSAGE,
  EMPTY_MESSAGE,
  EXPLANATION_INSTRUCTIONS,
  MAX_TOKENS,
  RELOAD_MESSAGE,
  TIMEOUT_MESSAGE,
  WALKTHROUGH_SCHEMA,
} from './register'

const SURFACES = ['terminal', 'desktop'] as const
const REF = (n: number) => n.toString(16).padStart(32, '0')

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
  rec(4, '2026-10-09T16:12:05Z', 'Worker reports the widget run complete.'),
]

function summary(overrides: Record<string, unknown> = {}, sessionId = 'sess-a') {
  return {
    schema: 'hermes-helmet.bridge.records/1',
    helmetVersion: '0.9.0',
    sessionId,
    readAt: '2026-10-09T17:20:00Z',
    fingerprint: '3:uuid-3',
    records: RECORDS,
    coverage: { recordsTotal: 3, recordsIncluded: 3, bytesOmitted: 0, unsupportedSkipped: 0, pendingTail: false },
    warnings: [],
    ...overrides,
  }
}

const ok = (payload: unknown) => ({ exitCode: 0, stdout: JSON.stringify(payload), stderr: '' })

// The valid, fully cited walkthrough a model reply can carry.
const GOOD = {
  objective: 'Ship the synthetic widget',
  summary: 'The widget was reported complete.',
  evidence: [REF(1)],
  items: [
    {
      id: 'widget',
      title: 'Add the synthetic widget',
      group: 'changed',
      status: 'Reported complete',
      summary: 'A widget was added in a pull request.',
      detail: 'The first officer opened the pull request; a worker reported the run complete.',
      evidence: [REF(1), REF(2), REF(4)],
      reported: { label: 'The worker reported the run complete', actor: 'Hermes', evidence: [REF(4)] },
      links: [
        { label: 'Widget PR', url: 'https://example.test/acme/widgets/pull/7', evidence: [REF(2)] },
      ],
    },
  ],
}

const text = (body: unknown = GOOD) => JSON.stringify(body)

// A mutable stand-in for the model plus the reader, and counters.
const fixture: {
  model: (call: any) => any
  read: any
  seed: { value: any; version: number } | null
  walkthrough: any
  gate: { resolve: (v: any) => void; reject: (e: any) => void } | null
} = { model: () => ({ value: { text: text() } }), read: null, seed: null, walkthrough: null, gate: null }

type Harness = {
  models: any[]
  runs: Array<{ argv: string[]; init: any }>
  prompts: any[]
  spawns: number
  sends: number
  tools: number
}

const SEED = {
  name: 'seed',
  register: (on: any) => {
    on('command.run', { command: 'probe' }, async ($: any) => {
      const request = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
      const walkthrough = await $.state.get({ plugin: 'hermes-helmet', key: 'walkthrough' })
      const records = await $.state.get({ plugin: 'hermes-helmet', key: 'records' })
      const view = await $.state.get({ plugin: 'hermes-helmet', key: 'view' })
      const binding = await $.state.get({ plugin: 'hermes-helmet', key: 'binding' })
      return {
        text: JSON.stringify({
          request: request.value ?? null,
          requestVersion: request.version,
          walkthrough: walkthrough.value ?? null,
          records: records.value ?? null,
          view: view.value ?? null,
          binding: binding.value ?? null,
        }),
      }
    })
  },
}
const WITH = { plugins: [SEED] }

function host(on: any, opts: { id?: string; listed?: any[] } = {}): Harness {
  const h: Harness = { models: [], runs: [], prompts: [], spawns: 0, sends: 0, tools: 0 }
  fixture.model = () => ({ value: { text: text() } })
  fixture.read = ok(summary())
  fixture.seed = null
  fixture.walkthrough = null
  fixture.gate = null
  on('state.get', async (_$: any, e: any, next: any) => {
    if (e.key === 'request' && fixture.seed !== null) {
      const seeded = fixture.seed
      fixture.seed = null
      return { value: { value: seeded.value, version: seeded.version } }
    }
    if (e.key === 'walkthrough' && fixture.walkthrough !== null) {
      return { value: { value: fixture.walkthrough, version: 1 } }
    }
    return next(e)
  })
  on('session.version', () => ({ value: { version: '2.1.296' } }))
  on('session.id', () => ({ value: opts.id ?? 'sess-a' }))
  on('session.start', () => ({ cwd: '/work' }))
  on('classic.SessionStart', () => ({}))
  on('command.register', (_$: any, e: any) => ({ value: { command: e.name } }))
  on('command.list', () => ({ value: opts.listed ?? [] }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('process.run', (_$: any, e: any) => {
    h.runs.push({ argv: [...e.argv], init: e.init })
    return { value: fixture.read }
  })
  on('model.complete', (_$: any, e: any) => {
    h.models.push(e)
    return fixture.model(e)
  })
  for (const event of ['prompt.submit', 'prompt.fill']) {
    on(event, (_$: any, e: any) => {
      h.prompts.push(e)
      return { text: e.text }
    })
  }
  for (const event of ['agent.spawn', 'session.send', 'tool.call', 'http.fetch', 'turn.start']) {
    on(event, () => {
      if (event === 'agent.spawn') h.spawns += 1
      if (event === 'session.send') h.sends += 1
      if (event === 'tool.call') h.tools += 1
      return {}
    })
  }
  return h
}

const START = { cwd: '/work', surface: 'terminal', isInteractive: true } as const
const tick = () => new Promise(resolve => setTimeout(resolve, 20))

function run(command: string, args = '') {
  return {
    command,
    args,
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 100 },
  } as any
}

async function open($: any, id = 'sess-a') {
  await $.classic.SessionStart({
    source: 'startup',
    session_id: id,
    transcript_path: `/synthetic/transcripts/${id}.jsonl`,
  })
  await $.command.run(run('captains-bridge'))
}

async function probe($: any): Promise<any> {
  return JSON.parse((await $.command.run(run('probe'))).text)
}

// Press Update and let exactly one request start on the mocked clock.
async function pressUpdate($: any, pane: any, clock: any) {
  await pane.press({ key: 'update-explanation' })
  await clock.advance(0)
  await tick()
}

function delayed(error?: () => Error) {
  fixture.model = () =>
    new Promise((resolve, reject) => {
      fixture.gate = { resolve, reject }
    }) as any
}

describe('starting an explanation request', () => {
  test('sends one tool-less request with the configured model, limit and document order', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    await pressUpdate($, pane, clock)

    expect(h.models.length).toBe(1)
    const call = h.models[0]
    expect(call.model).toBe('sonnet')
    expect(call.maxTokens).toBe(MAX_TOKENS)
    expect(call.timeoutMs).toBe(180000)
    expect(call.signal).toBeTruthy()
    expect(call.promptBlocks[0]).toEqual({ text: EXPLANATION_INSTRUCTIONS, cache: true })
    expect(call.promptBlocks[1].text).toBe(JSON.stringify(WALKTHROUGH_SCHEMA))
    const records = JSON.parse(call.promptBlocks[2].text)
    expect(records.sessionId).toBe('sess-a')
    expect(records.records.length).toBe(3)
    // No subagent, no turn, no prompt, no tool, no process spawn, no network.
    expect(h.spawns).toBe(0)
    expect(h.sends).toBe(0)
    expect(h.tools).toBe(0)

    // The valid, fully cited walkthrough installs and the request settles idle.
    const state = await probe($)
    expect(state.walkthrough.body.objective).toBe('Ship the synthetic widget')
    expect(state.walkthrough.readAt).toBe('2026-10-09T17:20:00Z')
    expect(state.request.status).toBe('idle')
    expect(await pane.find({ text: 'Ship the synthetic widget' })).not.toBe(undefined)
    await pane.unmount()
  })

  test('uses the configured model and time limit', { plugins: [SEED], options: { explanationModel: 'opus', explanationTimeoutSeconds: 30 } }, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    await pressUpdate($, pane, clock)
    expect(h.models[0].model).toBe('opus')
    expect(h.models[0].timeoutMs).toBe(30000)
    await pane.unmount()
  })

  test('ignores a duplicate Update press while preparing', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    // A second press while the first request is live is ignored.
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    expect(h.models.length).toBe(1)
    const state = await probe($)
    expect(state.request.status).toBe('preparing')
    expect(state.request.generation).toBe(1)
    await pane.unmount()
  })

  test('keeps the last walkthrough and reports the status while preparing', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    await pressUpdate($, pane, clock)
    // A second preparation, held open: the previous walkthrough stays drawn.
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    expect(h.models.length).toBe(2)
    expect(await pane.find({ key: 'cancel' })).not.toBe(undefined)
    expect(await pane.find({ key: 'keep-preparing' })).not.toBe(undefined)
    expect(await pane.find({ text: 'Ship the synthetic widget' })).not.toBe(undefined)
    await pane.unmount()
  })
})

describe('cancelling a request', () => {
  test('Cancel aborts, marks cancelled and leaves the walkthrough alone', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    await pressUpdate($, pane, clock)
    const before = await probe($)

    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    const signal = h.models[1].signal
    // Cancel settles the request at once and aborts it; the abort is what the
    // late reply below finds, and that reply can no longer install.
    expect(signal).toBeTruthy()
    await pane.press({ key: 'cancel' })
    expect(h.models.length).toBe(2)

    const state = await probe($)
    expect(state.request.status).toBe('cancelled')
    expect(state.walkthrough).toEqual(before.walkthrough)

    // A late reply of the cancelled request is silently discarded.
    fixture.gate.resolve({ value: { text: text({ ...GOOD, objective: 'A late objective' }) } })
    await tick()
    const after = await probe($)
    expect(after.request.status).toBe('cancelled')
    expect(after.walkthrough).toEqual(before.walkthrough)
    expect(await pane.find({ text: 'A late objective' })).toBe(undefined)
    await pane.unmount()
  })
})

describe('a person prompt during preparation', () => {
  test('supersedes the request and passes the prompt through unchanged', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    const signal = h.models[0].signal
    const prompt = { text: 'Start on the gadget instead.', origin: { kind: 'human' } }
    await (($ as any).prompt.submit(prompt))
    expect(h.prompts.length).toBe(1)
    expect(h.prompts[0].text).toBe('Start on the gadget instead.')
    expect(signal).toBeTruthy()
    const state = await probe($)
    expect(state.request.status).toBe('superseded')
    expect(state.walkthrough).toBe(null)

    fixture.gate.resolve({ value: { text: text() } })
    await tick()
    expect((await probe($)).walkthrough).toBe(null)
    await pane.unmount()
  })

  test('keeps the request when Keep preparing is on', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    await pane.press({ key: 'keep-preparing' })
    expect((await probe($)).request.retain).toBe(true)

    const signal = h.models[0].signal
    await (($ as any).prompt.submit({ text: 'Carry on.', origin: { kind: 'human' } }))
    expect(signal).toBeTruthy()
    expect((await probe($)).request.status).toBe('preparing')

    fixture.gate.resolve({ value: { text: text() } })
    await tick()
    expect((await probe($)).request.status).toBe('idle')
    await pane.unmount()
  })

  test('an unchanged prompt is passed on when no request is preparing', WITH, async ($, on) => {
    const h = host(on)
    await open($)
    const prompt = { text: 'A plain instruction.', origin: { kind: 'human' } }
    await (($ as any).prompt.submit(prompt))
    expect(h.prompts.length).toBe(1)
    expect(h.prompts[0].text).toBe('A plain instruction.')
    expect((await probe($)).request.message).toBe(undefined)
  })
})

describe('rejecting an explanation', () => {
  const BAD: Array<[string, any]> = [
    ['a non-JSON reply', 'not json at all'],
    ['a wrong shape', JSON.stringify({ objective: 'x' })],
    ['an empty top-level evidence list', text({ ...GOOD, evidence: [] })],
    ['an empty item evidence list', text({ ...GOOD, items: [{ ...GOOD.items[0], evidence: [] }] })],
    ['an unknown ref', text({ ...GOOD, evidence: [REF(99)] })],
    [
      'a link absent from its cited record',
      text({
        ...GOOD,
        items: [
          {
            ...GOOD.items[0],
            links: [{ label: 'Forged', url: 'https://example.test/not-in-records', evidence: [REF(2)] }],
          },
        ],
      }),
    ],
  ]

  for (const [name, reply] of BAD) {
    test(`rejects ${name} and keeps the previous walkthrough`, WITH, async ($, on) => {
      const h = host(on)
      const clock = (mock as any).clock(on)
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
      await pressUpdate($, pane, clock)
      const before = await probe($)

      fixture.model = () => ({ value: { text: reply } })
      await pane.press({ key: 'update-explanation' })
      await clock.advance(0)
      await tick()

      const state = await probe($)
      expect(h.models.length).toBe(2)
      expect(state.request.status).toBe('failed')
      expect(state.walkthrough).toEqual(before.walkthrough)
      expect(await pane.find({ text: DIFFICULT_MESSAGE })).not.toBe(undefined)
      expect(await pane.find({ text: 'Ship the synthetic widget' })).not.toBe(undefined)
      // A failed request never retries and never queues a later turn.
      await clock.advance(60000)
      await tick()
      expect(h.models.length).toBe(2)
      await pane.unmount()
    })
  }
})

describe('ending a request', () => {
  // The engine refuses a call with { deny: reason }, which is what the mod
  // sees for an api error and for its deadline abort.
  const ENDINGS: Array<[string, () => any, string, string]> = [
    ['a timeout', () => ({ deny: 'model.complete deadline reached' }), 'timed-out', TIMEOUT_MESSAGE],
    ['an api error', () => ({ deny: 'provider exploded' }), 'failed', 'provider exploded'],
    ['an empty reply', () => ({ value: { text: '   ' } }), 'failed', EMPTY_MESSAGE],
  ]

  for (const [name, reply, expectStatus, expectText] of ENDINGS) {
    test(`${name} ends in ${expectStatus} with exactly one request`, WITH, async ($, on) => {
      const h = host(on)
      const clock = (mock as any).clock(on)
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
      fixture.model = () => reply()
      await pane.press({ key: 'update-explanation' })
      await clock.advance(0)
      await tick()

      const state = await probe($)
      expect(state.request.status).toBe(expectStatus)
      expect(state.request.message ?? '').toContain(expectText)
      expect(h.models.length).toBe(1)
      expect(state.walkthrough).toBe(null)
      // No retry, and nothing queued for a later turn.
      await clock.advance(600000)
      await tick()
      expect(h.models.length).toBe(1)
      await pane.unmount()
    })
  }
})

describe('races and reloads', () => {
  test('a late reply of a superseded request never installs', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })

    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    const firstGate = fixture.gate

    // Cancel the first, then start a second request and let the first reply.
    await pane.press({ key: 'cancel' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    expect(h.models.length).toBe(2)

    firstGate.resolve({ value: { text: text({ ...GOOD, objective: 'The superseded objective' }) } })
    await tick()
    const during = await probe($)
    expect(during.request.status).toBe('preparing')
    expect(during.walkthrough).toBe(null)

    fixture.gate.resolve({ value: { text: text({ ...GOOD, objective: 'The second objective' }) } })
    await tick()
    const after = await probe($)
    expect(after.request.status).toBe('idle')
    expect(after.walkthrough.body.objective).toBe('The second objective')
    await pane.unmount()
  })

  test('a refresh during preparation keeps the request snapshot', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()

    // The records move on while the request is in flight.
    fixture.read = ok(
      summary({
        readAt: '2026-10-09T19:00:00Z',
        fingerprint: '6:uuid-6',
        coverage: { recordsTotal: 6, recordsIncluded: 6, bytesOmitted: 0, unsupportedSkipped: 0, pendingTail: false },
      }),
    )
    await pane.press({ key: 'refresh' })
    expect((await probe($)).records.readAt).toBe('2026-10-09T19:00:00Z')

    fixture.gate.resolve({ value: { text: text() } })
    await tick()
    const state = await probe($)
    expect(state.request.status).toBe('idle')
    // The explanation carries the readAt and fingerprint it was built from.
    expect(state.walkthrough.readAt).toBe('2026-10-09T17:20:00Z')
    expect(state.walkthrough.fingerprint).toBe('3:uuid-3')
    expect(h.models.length).toBe(1)
    await pane.unmount()
  })

  test('a reload during preparation ends the request as failed', WITH, async ($, on) => {
    host(on)
    await open($)
    // The reload left a preparing request with no live controller: seed the
    // read the mod makes at session.start with that state.
    const version = (await probe($)).requestVersion
    fixture.seed = { value: { generation: 3, status: 'preparing', retain: false }, version }
    await $.session.start(START)
    const state = await probe($)
    expect(state.request.status).toBe('failed')
    expect(state.request.message).toBe(RELOAD_MESSAGE)
  })

  test('a live preparation survives an unrelated session.start', WITH, async ($, on) => {
    host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    await $.session.start(START)
    expect((await probe($)).request.status).toBe('preparing')
    fixture.gate.resolve({ value: { text: text() } })
    await tick()
    expect((await probe($)).request.status).toBe('idle')
    await pane.unmount()
  })
})

describe('the drawn status', () => {
  for (const surface of SURFACES) {
    test(`shows the Update, Cancel and Keep preparing controls on ${surface}`, WITH, async ($, on) => {
      host(on)
      const clock = (mock as any).clock(on)
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      expect(await pane.find({ key: 'update-explanation' })).not.toBe(undefined)
      delayed()
      await pane.press({ key: 'update-explanation' })
      await clock.advance(0)
      await tick()
      expect(await pane.find({ key: 'cancel' })).not.toBe(undefined)
      expect(await pane.find({ key: 'keep-preparing' })).not.toBe(undefined)
      expect(
        await pane.find({ text: 'Preparing a new explanation in the background.' }),
      ).not.toBe(undefined)
      await pane.unmount()
    })
  }
})
