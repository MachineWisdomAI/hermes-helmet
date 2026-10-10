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

const ZERO_USAGE = {
  input_tokens: 0,
  output_tokens: 0,
  cache_read_input_tokens: 0,
  cache_creation_input_tokens: 0,
}

// What the engine resolves for an answered call (ModelCompleteResult's first arm).
const answered = (body: string) => ({ value: { isAnswered: true, text: body, usage: ZERO_USAGE } })

// A mutable stand-in for the model plus the reader, and counters. `holdRead`
// suspends `helmet bridge read` so a test can act while a preparation waits on
// it; `releaseRead` lets it answer. `holdSet` suspends the first
// `$.state.set` its predicate matches, so a test can interleave a competing
// write with a settling transition and release it afterwards.
const fixture: {
  model: (call: any) => any
  read: any
  seed: { value: any; version: number } | null
  walkthrough: any
  gate: { resolve: (v: any) => void; reject: (e: any) => void } | null
  holdRead: boolean
  releaseRead: (() => void) | null
  holdSet: ((e: any) => boolean) | null
  releaseSet: (() => void) | null
} = {
  model: () => answered(text()),
  read: null,
  seed: null,
  walkthrough: null,
  gate: null,
  holdRead: false,
  releaseRead: null,
  holdSet: null,
  releaseSet: null,
}

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
  fixture.model = () => answered(text())
  fixture.read = ok(summary())
  fixture.seed = null
  fixture.walkthrough = null
  fixture.gate = null
  fixture.holdRead = false
  fixture.releaseRead = null
  fixture.holdSet = null
  fixture.releaseSet = null
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
  on('state.set', async (_$: any, e: any, next: any) => {
    // Hold the first matching write so a test can interleave a competing one.
    if (fixture.holdSet !== null && fixture.holdSet(e)) {
      fixture.holdSet = null
      await new Promise<void>(resolve => {
        fixture.releaseSet = resolve
      })
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
    if (!fixture.holdRead) return { value: fixture.read }
    return new Promise(resolve => {
      fixture.releaseRead = () => resolve({ value: fixture.read })
    })
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

// The AbortSignal is the call's own option (ModelCompleteOptions), never a
// request field: the hook input, the request as the engine read it, carries
// no `signal`.
function carriesSignal(call: any): boolean {
  return Object.prototype.hasOwnProperty.call(call, 'signal')
}

// Hold `helmet bridge read` so a press is suspended after it claimed the
// request and before any model call is scheduled.
function heldRead() {
  fixture.holdRead = true
}

function releaseRead() {
  if (fixture.releaseRead !== null) fixture.releaseRead()
}

// Hold the first `$.state.set` that matches `match`, so a test can land a
// competing writing press against a transition that is already settling, then
// release it. Only the first match is held; a retried write passes through.
function heldSet(match: (e: any) => boolean) {
  fixture.holdSet = match
}

function releaseSet() {
  if (fixture.releaseSet !== null) {
    const release = fixture.releaseSet
    fixture.releaseSet = null
    release()
  }
}

// The request write that settles a completed preparation to idle.
const isRequestIdle = (e: any): boolean =>
  e.key === 'request' && e.value?.status === 'idle'

// The request write that marks a request cancelled.
const isRequestCancelled = (e: any): boolean =>
  e.key === 'request' && e.value?.status === 'cancelled'

// The walkthrough write that adopts a completed reply.
const isWalkthroughWrite = (e: any): boolean => e.key === 'walkthrough'

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
    // The AbortSignal is the call's own option, not a request field.
    expect(carriesSignal(call)).toBe(false)
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
    // Cancel settles the request at once and aborts the call's own signal; the
    // late reply below can no longer install.
    expect(carriesSignal(h.models[1])).toBe(false)
    await pane.press({ key: 'cancel' })
    expect(h.models.length).toBe(2)

    const state = await probe($)
    expect(state.request.status).toBe('cancelled')
    expect(state.walkthrough).toEqual(before.walkthrough)

    // A late reply of the cancelled request is silently discarded.
    fixture.gate.resolve(answered(text({ ...GOOD, objective: 'A late objective' })))
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
    const prompt = { text: 'Start on the gadget instead.', origin: { kind: 'human' } }
    await (($ as any).prompt.submit(prompt))
    expect(h.prompts.length).toBe(1)
    expect(h.prompts[0].text).toBe('Start on the gadget instead.')
    expect(carriesSignal(h.models[0])).toBe(false)
    const state = await probe($)
    expect(state.request.status).toBe('superseded')
    expect(state.walkthrough).toBe(null)

    fixture.gate.resolve(answered(text()))
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

    await (($ as any).prompt.submit({ text: 'Carry on.', origin: { kind: 'human' } }))
    expect(carriesSignal(h.models[0])).toBe(false)
    expect((await probe($)).request.status).toBe('preparing')

    fixture.gate.resolve(answered(text()))
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

      fixture.model = () => answered(reply)
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
  // The engine never rejects over what the provider did: it resolves a result.
  // Its time limit and the call's own abort arrive as `aborted`, a provider
  // failure as `api-error` with the status and the classified kind, a reply
  // with no text as `empty-reply`. Only an engine refusal rejects.
  const ZERO = { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }
  const ENDINGS: Array<[string, () => any, string, string]> = [
    ['its time limit', () => ({ value: { isAnswered: false, reason: 'aborted', usage: ZERO } }), 'timed-out', TIMEOUT_MESSAGE],
    ['a provider error', () => ({ value: { isAnswered: false, reason: 'api-error', status: 529, error: 'overloaded', usage: ZERO } }), 'failed', 'overloaded'],
    ['a reply with no text', () => ({ value: { isAnswered: false, reason: 'empty-reply', usage: ZERO } }), 'failed', EMPTY_MESSAGE],
    // An engine refusal (a blocked model, a bad cap) is the one case that rejects.
    ['an engine refusal', () => ({ deny: 'the model is blocked' }), 'failed', 'the model is blocked'],
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

  test('a provider error is not described as an empty reply', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    fixture.model = () => ({
      value: { isAnswered: false, reason: 'api-error', status: 500, error: 'server_error', usage: ZERO },
    })
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()

    const message = (await probe($)).request.message ?? ''
    expect(message).toContain('server_error')
    expect(message).toContain('500')
    expect(message).not.toContain(EMPTY_MESSAGE)
    expect(h.models.length).toBe(1)
    await pane.unmount()
  })

  test('a provider error with no response names no status', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    fixture.model = () => ({
      value: { isAnswered: false, reason: 'api-error', status: null, error: 'unknown', usage: ZERO },
    })
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()

    const state = await probe($)
    expect(state.request.status).toBe('failed')
    expect(state.request.message ?? '').toContain('no response arrived')
    expect(h.models.length).toBe(1)
    await pane.unmount()
  })
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

    firstGate.resolve(answered(text({ ...GOOD, objective: 'The superseded objective' })))
    await tick()
    const during = await probe($)
    expect(during.request.status).toBe('preparing')
    expect(during.walkthrough).toBe(null)

    fixture.gate.resolve(answered(text({ ...GOOD, objective: 'The second objective' })))
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

    fixture.gate.resolve(answered(text()))
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
    fixture.gate.resolve(answered(text()))
    await tick()
    expect((await probe($)).request.status).toBe('idle')
    await pane.unmount()
  })
})

// A settling transition writes the request under compare-and-set. A competing
// press (Keep preparing) that lands between its read and its write must not
// strand the request or lose a cancellation: the transition re-reads and
// retries while it still owns the generation. An older adoption must not
// retry over a newer accepted explanation.
describe('compare-and-set collisions', () => {
  test('a Keep preparing press during result adoption does not strand the request', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()

    // Hold the write that settles the completed reply to idle, then let a Keep
    // preparing press bump the store version under it.
    heldSet(isRequestIdle)
    fixture.gate.resolve(answered(text()))
    await tick()
    expect(fixture.releaseSet).not.toBe(null)
    await pane.press({ key: 'keep-preparing' })
    releaseSet()
    await tick()

    const state = await probe($)
    expect(state.request.status).toBe('idle')
    expect(state.request.retain).toBe(true)
    expect(state.walkthrough.body.objective).toBe('Ship the synthetic widget')
    expect(h.models.length).toBe(1)
    await pane.unmount()
  })

  test('a Keep preparing press during Cancel still advances the generation', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()

    // Hold Cancel's write, bump the version with Keep preparing, then release:
    // the cancel must not miss and let the cancelled reply install.
    heldSet(isRequestCancelled)
    const cancelling = pane.press({ key: 'cancel' })
    await tick()
    expect(fixture.releaseSet).not.toBe(null)
    await pane.press({ key: 'keep-preparing' })
    releaseSet()
    await cancelling

    fixture.gate.resolve(answered(text({ ...GOOD, objective: 'A cancelled objective' })))
    await tick()
    const state = await probe($)
    expect(state.request.status).toBe('cancelled')
    expect(state.request.generation).toBe(2)
    expect(state.walkthrough).toBe(null)
    expect(await pane.find({ text: 'A cancelled objective' })).toBe(undefined)
    await pane.unmount()
  })

  test('an older adoption never overwrites a newer accepted explanation', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    delayed()
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()

    // Hold the first reply's walkthrough write. The request stays preparing, so
    // a second Update is ignored rather than racing the held write.
    heldSet(isWalkthroughWrite)
    fixture.gate.resolve(answered(text({ ...GOOD, objective: 'First objective' })))
    await tick()
    expect(fixture.releaseSet).not.toBe(null)
    await pane.press({ key: 'update-explanation' })
    await clock.advance(0)
    await tick()
    expect(h.models.length).toBe(1)

    releaseSet()
    await tick()
    let state = await probe($)
    expect(state.request.status).toBe('idle')
    expect(state.walkthrough.body.objective).toBe('First objective')

    // The next explicit Update still succeeds.
    fixture.model = () => answered(text({ ...GOOD, objective: 'Second objective' }))
    await pressUpdate($, pane, clock)
    state = await probe($)
    expect(state.request.status).toBe('idle')
    expect(state.walkthrough.body.objective).toBe('Second objective')
    expect(h.models.length).toBe(2)
    await pane.unmount()
  })

  test('Cancel during a held walkthrough write keeps the last useful explanation', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    fixture.model = () => answered(text({ ...GOOD, objective: 'Useful previous explanation' }))
    await pressUpdate($, pane, clock)
    expect((await probe($)).walkthrough.body.objective).toBe('Useful previous explanation')

    delayed()
    await pressUpdate($, pane, clock)
    heldSet(isWalkthroughWrite)
    fixture.gate.resolve(answered(text({ ...GOOD, objective: 'Cancelled replacement' })))
    await tick()
    expect(fixture.releaseSet).not.toBe(null)
    expect((await probe($)).request.status).toBe('preparing')

    await pane.press({ key: 'cancel' })
    expect((await probe($)).request.status).toBe('cancelled')
    releaseSet()
    await tick()
    await clock.advance(0)
    await tick()

    const state = await probe($)
    expect(state.request.status).toBe('cancelled')
    expect(state.walkthrough.body.objective).toBe('Useful previous explanation')
    expect(h.models.length).toBe(2)
    await pane.unmount()
  })

  test('a cancelled older result cannot replace a newer accepted explanation', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })
    fixture.model = () => answered(text({ ...GOOD, objective: 'Useful previous explanation' }))
    await pressUpdate($, pane, clock)

    delayed()
    await pressUpdate($, pane, clock)
    heldSet(isWalkthroughWrite)
    fixture.gate.resolve(answered(text({ ...GOOD, objective: 'Cancelled older replacement' })))
    await tick()
    expect(fixture.releaseSet).not.toBe(null)
    await pane.press({ key: 'cancel' })

    // A newer explicit Update completes while the older write is still held.
    fixture.model = () => answered(text({ ...GOOD, objective: 'Fresh accepted explanation' }))
    await pressUpdate($, pane, clock)
    let state = await probe($)
    expect(state.walkthrough.body.objective).toBe('Fresh accepted explanation')

    releaseSet()
    await tick()
    state = await probe($)
    expect(state.request.status).toBe('idle')
    expect(state.walkthrough.body.objective).toBe('Fresh accepted explanation')
    await pane.unmount()
  })
})

describe('a preparation ended while the reader runs', () => {
  test('Cancel during preparation starts no request and stays cancelled', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })

    // Hold the reader: the press is suspended after it claimed the request,
    // before any model call is scheduled.
    heldRead()
    const pressing = pane.press({ key: 'update-explanation' })
    await tick()
    expect((await probe($)).request.status).toBe('preparing')
    expect(await pane.find({ key: 'cancel' })).not.toBe(undefined)

    await pane.press({ key: 'cancel' })
    expect((await probe($)).request.status).toBe('cancelled')

    // The held reader answers: no stale request launches off the cancelled
    // preparation, and no later call follows.
    releaseRead()
    await pressing
    await clock.advance(0)
    await tick()
    expect(h.models.length).toBe(0)
    await clock.advance(600000)
    await tick()
    expect(h.models.length).toBe(0)
    expect((await probe($)).request.status).toBe('cancelled')
    await pane.unmount()
  })

  test('a prompt during preparation starts no request and stays superseded', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })

    heldRead()
    const pressing = pane.press({ key: 'update-explanation' })
    await tick()
    expect((await probe($)).request.status).toBe('preparing')

    await (($ as any).prompt.submit({ text: 'Start on the gadget.', origin: { kind: 'human' } }))
    expect((await probe($)).request.status).toBe('superseded')

    releaseRead()
    await pressing
    await clock.advance(0)
    await tick()
    expect(h.models.length).toBe(0)
    expect((await probe($)).request.status).toBe('superseded')
    await pane.unmount()
  })

  test('a held preparation still starts exactly one request when nothing ends it', WITH, async ($, on) => {
    const h = host(on)
    const clock = (mock as any).clock(on)
    await open($)
    const pane = await $.ui.mount({ ...PANE, surface: 'terminal' })

    heldRead()
    const pressing = pane.press({ key: 'update-explanation' })
    await tick()
    releaseRead()
    await pressing
    await clock.advance(0)
    await tick()
    expect(h.models.length).toBe(1)
    expect(carriesSignal(h.models[0])).toBe(false)
    expect((await probe($)).request.status).toBe('idle')
    await pane.unmount()
  })
})

describe('the call’s own AbortSignal option', () => {
  // The engine reads a cancellation only from the call's own options; a
  // `signal` inside the request reaches the hook input and aborts nothing.
  const SIGNAL_PROBE = {
    name: 'signal-probe',
    register: (on: any) => {
      on('command.run', { command: 'probe-aborted' }, async ($: any) => {
        const controller = new AbortController()
        controller.abort()
        const result = await $.model.complete(
          { model: 'sonnet', prompt: 'hello' },
          { signal: controller.signal },
        )
        return { text: JSON.stringify({ result }) }
      })
    },
  }

  test('an aborted own signal settles the call aborted without dispatching', { plugins: [SEED, SIGNAL_PROBE] }, async ($, on) => {
    const h = host(on)
    const result = JSON.parse((await $.command.run(run('probe-aborted'))).text).result
    expect(result.isAnswered).toBe(false)
    expect(result.reason).toBe('aborted')
    // Nothing reached the model hook: the engine cut it before the dispatch.
    expect(h.models.length).toBe(0)
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
