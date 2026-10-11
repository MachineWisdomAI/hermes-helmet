// Tests for Show Me and Retro. They stand in for $.command.list and
// $.command.run, mount the pane on the terminal and the desktop and press the
// keyed buttons. Every fixture is synthetic.
import { describe, expect, test } from 'claude-code/testing'
import { availabilityOf, missingMessage, scopeArgs } from './register'

const SURFACES = ['terminal', 'desktop'] as const
const REF = (n: number) => n.toString(16).padStart(32, '0')

const PANE = {
  plugin: 'hermes-helmet',
  component: 'Pane',
  requestId: 'captains-bridge',
  props: { title: "Captain's Bridge", isFocused: true, bodyColumns: 80, placement: 'dock' },
} as const

const SHOW_ME_MISSING =
  "Show Me isn't installed. Install a skill named show-me, or set showMeCommand."
const RETRO_MISSING = "Retro isn't installed. Install a skill named retro, or set retroCommand."

const RECORD_LIST = [
  { ref: REF(1), parentRef: null, agentId: null, timestamp: '2026-10-09T16:00:00Z', role: 'user', origin: 'person', text: 'Add the widget.', truncated: false },
  { ref: REF(2), parentRef: null, agentId: null, timestamp: '2026-10-09T16:02:30Z', role: 'assistant', origin: 'assistant', text: 'Opened the PR.', truncated: false },
]
const COVERAGE = { recordsTotal: 2, recordsIncluded: 2, bytesOmitted: 0, unsupportedSkipped: 0, pendingTail: false }
const RECORDS = {
  fingerprint: '2:uuid-2',
  readAt: '2026-10-09T17:20:00Z',
  coverage: COVERAGE,
  summary: {
    schema: 'hermes-helmet.bridge.records/1',
    helmetVersion: '0.9.0',
    sessionId: 'sess-a',
    readAt: '2026-10-09T17:20:00Z',
    fingerprint: '2:uuid-2',
    records: RECORD_LIST,
    coverage: COVERAGE,
    warnings: [],
  },
}

const WALKTHROUGH = {
  body: {
    objective: 'Ship the synthetic widget',
    summary: 'The widget was added.',
    evidence: [REF(1)],
    items: [
      { id: 'widget', title: 'Add the synthetic widget', group: 'changed', status: 'Merged', summary: 's', detail: 'd', evidence: [REF(1), REF(2)] },
    ],
  },
  readAt: '2026-10-09T17:00:00Z',
  fingerprint: '2:uuid-2',
  preparedAt: '2026-10-09T17:01:00Z',
}

// Scope plumbing in the skeleton state: this slice reads the selected item
// from `view`, so it is testable before the overview/detail slice lands. A
// stand-in answers the mod's read of that selection.
const fixture: { selected: string | null; seeded: boolean } = { selected: null, seeded: true }

type Harness = {
  listed: { current: any }
  listCalls: number
  runs: Array<{ command: string; args: string }>
  turns: number
  models: number
  gates: Array<() => void>
  mode: { current: 'immediate' | 'delayed' | 'unknown' | 'error' }
}

function host(on: any, opts: { listed?: any; seeded?: boolean } = {}): Harness {
  const h: Harness = {
    listed: { current: opts.listed ?? [] },
    listCalls: 0,
    runs: [],
    turns: 0,
    models: 0,
    gates: [],
    mode: { current: 'immediate' },
  }
  fixture.selected = null
  fixture.seeded = opts.seeded ?? true
  on('state.get', async (_$: any, e: any, next: any) => {
    if (fixture.seeded && e.key === 'walkthrough') return { value: { value: WALKTHROUGH, version: 1 } }
    if (fixture.seeded && e.key === 'records') return { value: { value: RECORDS, version: 1 } }
    if (e.key === 'view' && fixture.selected !== null) {
      return { value: { value: { stack: [{ itemId: fixture.selected }] }, version: 1 } }
    }
    return next(e)
  })
  on('session.version', () => ({ value: { version: '2.1.296' } }))
  on('session.id', () => ({ value: 'sess-a' }))
  on('session.start', () => ({ cwd: '/work' }))
  on('classic.SessionStart', () => ({}))
  on('command.register', (_$: any, e: any) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('command.list', () => {
    h.listCalls += 1
    return { value: h.listed.current }
  })
  on('command.run', async (_$: any, e: any) => {
    if (e.command === 'captains-bridge') return { text: 'open' }
    h.runs.push({ command: e.command, args: e.args })
    if (h.mode.current === 'unknown') throw new Error(`$.command.run: no command named /${e.command} in this session`)
    if (h.mode.current === 'error') throw new Error('queue refused')
    if (h.mode.current === 'delayed') await new Promise<void>(resolve => h.gates.push(resolve))
    return { text: 'queued' }
  })
  for (const event of ['prompt.submit', 'prompt.fill', 'session.send', 'agent.spawn']) {
    on(event, () => {
      h.turns += 1
      return {}
    })
  }
  on('model.complete', () => {
    h.models += 1
    return {}
  })
  return h
}

function open($: any) {
  return $.command.run({
    command: 'captains-bridge',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns: 100 },
  } as any)
}

const names = (...n: string[]) => n.map(name => ({ name, description: 'x' }))
const tick = () => new Promise(resolve => setTimeout(resolve, 20))

describe('availability', () => {
  const defaults = { 'show-me': 'show-me', retro: 'retro' }

  test('unqualified, namespaced and configured names are accepted', () => {
    expect(availabilityOf(names('show-me', 'retro'), defaults)).toEqual({ 'show-me': 'show-me', retro: 'retro' })
    expect(availabilityOf(names('captain-kit:show-me', 'captain-kit:retro'), defaults)).toEqual({
      'show-me': 'captain-kit:show-me',
      retro: 'captain-kit:retro',
    })
    expect(availabilityOf(names('walk', 'look-back'), { 'show-me': 'walk', retro: 'look-back' })).toEqual({
      'show-me': 'walk',
      retro: 'look-back',
    })
    expect(availabilityOf({ commands: names('retro') }, defaults)).toEqual({ 'show-me': null, retro: 'retro' })
  })

  test('similar names do not match', () => {
    expect(availabilityOf(names('show-me-more', 'x:retro-old', 'noretro'), defaults)).toEqual({
      'show-me': null,
      retro: null,
    })
  })

  test('the missing wording names the skill and the setting', () => {
    expect(missingMessage('show-me', 'show-me')).toBe(SHOW_ME_MISSING)
    expect(missingMessage('retro', 'retro')).toBe(RETRO_MISSING)
  })
})

describe('scope', () => {
  test('is the whole session without a selected item', () => {
    expect(scopeArgs(null, null)).toBe('this session')
  })

  test('is the item title with evidence refs and cited timestamps', () => {
    const args = scopeArgs({ title: 'Add the synthetic widget', evidence: [REF(1), REF(2), REF(9)] }, RECORD_LIST)
    expect(args).toContain('Add the synthetic widget')
    expect(args).toContain(`${REF(1)} (2026-10-09T16:00:00Z)`)
    expect(args).toContain(`${REF(2)} (2026-10-09T16:02:30Z)`)
    expect(args).toContain(REF(9))
  })
})

for (const surface of SURFACES) {
  describe(`Show Me and Retro on ${surface}`, () => {
    test('are disabled with the missing wording and send nothing', async ($, on) => {
      const h = host(on)
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      expect(await pane.find({ text: SHOW_ME_MISSING })).not.toBe(undefined)
      expect(await pane.find({ text: RETRO_MISSING })).not.toBe(undefined)
      expect(await pane.find({ key: 'show-me' })).toBe(undefined)
      expect(await pane.find({ key: 'retro' })).toBe(undefined)
      expect(await pane.find({ text: "Captain's Bridge" })).not.toBe(undefined)
      expect(h.runs).toEqual([])
      await pane.unmount()
    })

    test('discovery runs when the Bridge opens', async ($, on) => {
      const h = host(on, { listed: names('show-me') })
      await open($)
      expect(h.listCalls).toBeGreaterThan(0)
      const pane = await $.ui.mount({ ...PANE, surface })
      expect(await pane.find({ key: 'show-me' })).not.toBe(undefined)
      expect(await pane.find({ text: RETRO_MISSING })).not.toBe(undefined)
      await pane.unmount()
    })

    test('a press runs the whole-session scope once, with no prompt or subagent', async ($, on) => {
      const h = host(on, { listed: names('show-me', 'retro'), seeded: false })
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      await pane.press({ key: 'show-me' })
      expect(h.runs).toEqual([{ command: 'show-me', args: 'this session' }])
      await pane.press({ key: 'retro' })
      expect(h.runs[1]).toEqual({ command: 'retro', args: 'this session' })
      expect(h.turns).toBe(0)
      expect(h.models).toBe(0)
      await pane.unmount()
    })

    test('a namespaced command runs under its listed name', async ($, on) => {
      const h = host(on, { listed: names('captain-kit:retro') })
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      await pane.press({ key: 'retro' })
      expect(h.runs).toEqual([{ command: 'captain-kit:retro', args: 'this session' }])
      await pane.unmount()
    })

    test('the selected item scope carries title, refs and timestamps', async ($, on) => {
      const h = host(on, { listed: names('show-me', 'retro') })
      await open($)
      fixture.selected = 'widget'
      const pane = await $.ui.mount({ ...PANE, surface })
      await pane.press({ key: 'retro' })
      expect(h.runs.length).toBe(1)
      expect(h.runs[0].command).toBe('retro')
      expect(h.runs[0].args).toContain('Add the synthetic widget')
      expect(h.runs[0].args).toContain(`${REF(1)} (2026-10-09T16:00:00Z)`)
      expect(h.runs[0].args).toContain(`${REF(2)} (2026-10-09T16:02:30Z)`)
      await pane.unmount()
    })

    test('one queued call during delayed settlement; repeat presses are ignored', async ($, on) => {
      const h = host(on, { listed: names('show-me', 'retro') })
      h.mode.current = 'delayed'
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      const first = pane.press({ key: 'show-me' })
      await tick()
      expect(h.runs.length).toBe(1)
      await pane.press({ key: 'show-me' })
      await pane.press({ key: 'show-me' })
      // Retro shares the single pending slot while Show Me is unsettled.
      await pane.press({ key: 'retro' })
      expect(h.runs.length).toBe(1)
      h.gates.forEach(release => release())
      await first
      h.mode.current = 'immediate'
      await pane.press({ key: 'show-me' })
      expect(h.runs.length).toBe(2)
      expect(h.turns).toBe(0)
      await pane.unmount()
    })

    test('an error settles pending, shows a notice and keeps the button', async ($, on) => {
      const h = host(on, { listed: names('show-me') })
      h.mode.current = 'error'
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      await pane.press({ key: 'show-me' })
      expect(await pane.find({ text: "Show Me couldn't be queued. Try again." })).not.toBe(undefined)
      expect(await pane.find({ key: 'show-me' })).not.toBe(undefined)
      h.mode.current = 'immediate'
      await pane.press({ key: 'show-me' })
      expect(h.runs.length).toBe(2)
      expect(await pane.find({ text: "Show Me couldn't be queued. Try again." })).toBe(undefined)
      await pane.unmount()
    })

    test('an unknown-name rejection returns to the missing state', async ($, on) => {
      const h = host(on, { listed: names('show-me', 'retro') })
      h.mode.current = 'unknown'
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      // The host rejects the name and no longer lists it.
      h.listed.current = names('retro')
      await pane.press({ key: 'show-me' })
      expect(h.runs.length).toBe(1)
      expect(await pane.find({ text: SHOW_ME_MISSING })).not.toBe(undefined)
      expect(await pane.find({ key: 'show-me' })).toBe(undefined)
      expect(await pane.find({ key: 'retro' })).not.toBe(undefined)
      expect(await pane.find({ text: "Captain's Bridge" })).not.toBe(undefined)
      await pane.unmount()
    })

    test('the explanation stays intact around a press', async ($, on) => {
      const h = host(on, { listed: names('show-me') })
      await open($)
      // Overview: the walkthrough is drawn with its item, then an item press
      // from the actual pane opens the detail and the action keeps it intact.
      const pane = await $.ui.mount({ ...PANE, surface })
      expect(await pane.find({ text: 'Ship the synthetic widget' })).not.toBe(undefined)
      expect(await pane.find({ key: 'item:widget' })).not.toBe(undefined)
      await pane.press({ key: 'item:widget' })
      await pane.press({ key: 'show-me' })
      expect(h.runs.length).toBe(1)
      expect(h.runs[0].args).toContain('Add the synthetic widget')
      expect(await pane.find({ text: 'Explanation' })).not.toBe(undefined)
      expect(await pane.find({ text: 'Add the synthetic widget' })).not.toBe(undefined)
      await pane.unmount()
    })

    test('every Refresh records press discovers commands again', async ($, on) => {
      const h = host(on, { listed: names() })
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      expect(await pane.find({ key: 'show-me' })).toBe(undefined)
      const before = h.listCalls
      h.listed.current = names('show-me')
      await pane.press({ key: 'refresh' })
      expect(h.listCalls).toBeGreaterThan(before)
      await pane.press({ key: 'refresh' })
      expect(h.listCalls).toBeGreaterThan(before + 1)
      await pane.press({ key: 'show-me' })
      expect(h.runs.length).toBe(1)
      await pane.unmount()
    })

    test('the item opened in the real pane sets the scope, and back returns to the session', async ($, on) => {
      const h = host(on, { listed: names('show-me', 'retro') })
      await open($)
      const pane = await $.ui.mount({ ...PANE, surface })
      await pane.press({ key: 'item:widget' })
      await pane.press({ key: 'retro' })
      expect(h.runs.length).toBe(1)
      expect(h.runs[0].args).toContain('Add the synthetic widget')
      expect(h.runs[0].args).toContain(`${REF(1)} (2026-10-09T16:00:00Z)`)
      expect(h.runs[0].args).toContain(`${REF(2)} (2026-10-09T16:02:30Z)`)
      await pane.press({ key: 'back' })
      await pane.press({ key: 'show-me' })
      expect(h.runs[1]).toEqual({ command: 'show-me', args: 'this session' })
      await pane.unmount()
    })
  })
}
