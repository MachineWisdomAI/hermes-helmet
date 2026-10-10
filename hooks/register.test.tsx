// Tests for the Captain's Bridge mod skeleton. They drive only the public
// seams of claude-code/testing: classic events, the command, a drawn pane on
// terminal and desktop, and the non-drawing text fallback. Every fixture is
// synthetic.
import { describe, expect, test } from 'claude-code/testing'
import { overviewText } from './register'

const CHANGED =
  'This conversation changed. Run /captains-bridge to open the Bridge for it.'
const SURFACES = ['terminal', 'desktop'] as const

// A stand-in plugin that reads the mod's state back as a person-visible
// string, so tests assert on observable state without the mod's internals.
const PROBE = {
  name: 'probe',
  register: (on: any) => {
    on('command.run', { command: 'probe' }, async ($: any) => {
      const binding = await $.state.get({ plugin: 'hermes-helmet', key: 'binding' })
      const request = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
      return { text: JSON.stringify({ binding: binding.value ?? null, request: request.value ?? null }) }
    })
  },
}
const WITH_PROBE = { plugins: [PROBE] }

const PANE = {
  plugin: 'hermes-helmet',
  component: 'Pane',
  requestId: 'captains-bridge',
  props: { title: "Captain's Bridge", isFocused: true, bodyColumns: 60, placement: 'dock' },
} as const

// The bottom of the chain stands for the engine; these answers are synthetic.
function host(
  on: any,
  opts: { version?: string; id?: string; isPlaced?: boolean } = {},
) {
  const seen = { opened: [] as any[], registered: [] as any[], turns: 0, timers: 0, reached: [] as any[] }
  on('session.version', () => ({ value: { version: opts.version ?? '2.1.296' } }))
  on('session.id', () => ({ value: opts.id ?? 'sess-a' }))
  on('session.start', () => ({ cwd: '/work' }))
  on('session.end', (_$: any, e: any) => ({ sessionId: e.sessionId }))
  on('classic.SessionStart', (_$: any, e: any) => {
    seen.reached.push(e)
    return {}
  })
  on('command.register', (_$: any, e: any) => {
    seen.registered.push(e)
    return { value: { command: e.name } }
  })
  on('ui.open', (_$: any, e: any) => {
    seen.opened.push(e)
    return { value: { isPlaced: opts.isPlaced ?? true } }
  })
  on('prompt.submit', () => {
    seen.turns += 1
    return {}
  })
  on('session.send', () => {
    seen.turns += 1
    return {}
  })
  on('clock.after', () => {
    seen.timers += 1
    return {}
  })
  on('clock.every', () => {
    seen.timers += 1
    return {}
  })
  return seen
}

const START = { cwd: '/work', surface: 'terminal', isInteractive: true } as const

function run(command: string, args = '', columns = 120) {
  return {
    command,
    args,
    origin: { kind: 'composer' },
    presentation: { isFullscreen: true, columns },
  } as any
}

async function stateOf($: any, key: 'binding' | 'request'): Promise<any> {
  const out = await $.command.run(run('probe'))
  return JSON.parse(out.text)[key]
}

describe('session.start', () => {
  test('registers /captains-bridge as an immediate command', WITH_PROBE, async ($, on) => {
    const seen = host(on)
    await $.session.start(START)
    expect(seen.registered.length).toBe(1)
    expect(seen.registered[0].name).toBe('captains-bridge')
    expect(seen.registered[0].immediate).toBe(true)
    expect(seen.opened.length).toBe(0)
  })

  test('retains the exact session id and never auto-opens', WITH_PROBE, async ($, on) => {
    const seen = host(on, { id: 'sess-start' })
    await $.session.start(START)
    expect(await stateOf($, 'binding')).toEqual({ sessionId: 'sess-start' })
    expect(seen.opened).toEqual([])
  })
})

describe('classic.SessionStart', () => {
  test('retains the session id and transcript path', WITH_PROBE, async ($, on) => {
    host(on)
    await $.classic.SessionStart({
      source: 'startup',
      session_id: 'sess-a',
      transcript_path: '/synthetic/transcripts/sess-a.jsonl',
    })
    expect(await stateOf($, 'binding')).toEqual({
      sessionId: 'sess-a',
      transcriptPath: '/synthetic/transcripts/sess-a.jsonl',
    })
  })

  test('a reload keeps the established binding for the same session', WITH_PROBE, async ($, on) => {
    host(on, { id: 'sess-a' })
    await $.classic.SessionStart({
      source: 'startup',
      session_id: 'sess-a',
      transcript_path: '/synthetic/transcripts/sess-a.jsonl',
    })
    await $.session.start(START)
    expect(await stateOf($, 'binding')).toEqual({
      sessionId: 'sess-a',
      transcriptPath: '/synthetic/transcripts/sess-a.jsonl',
    })
  })

  test('a reload never rebinds to a different session silently', WITH_PROBE, async ($, on) => {
    host(on, { id: 'sess-b' })
    await $.classic.SessionStart({
      source: 'startup',
      session_id: 'sess-a',
      transcript_path: '/synthetic/transcripts/sess-a.jsonl',
    })
    await $.session.start(START)
    expect(await stateOf($, 'binding')).toBe(null)
  })

  for (const source of ['clear', 'resume', 'fork'] as const) {
    test(`${source} ends the binding and does not rebind`, WITH_PROBE, async ($, on) => {
      const seen = host(on)
      await $.classic.SessionStart({ source: 'startup', session_id: 'sess-a' })
      await $.classic.SessionStart({ source, session_id: 'sess-new' })
      expect(await stateOf($, 'binding')).toBe(null)
      expect((await stateOf($, 'request')).message).toBe(CHANGED)
      expect(seen.opened).toEqual([])
    })
  }

  for (const source of ['clear', 'resume', 'fork'] as const) {
    test(`${source} stays ended through a plugin reload`, WITH_PROBE, async ($, on) => {
      host(on, { id: 'sess-new' })
      await $.classic.SessionStart({ source: 'startup', session_id: 'sess-a' })
      await $.classic.SessionStart({ source, session_id: 'sess-new' })
      await $.session.start(START)
      expect(await stateOf($, 'binding')).toBe(null)
      expect((await stateOf($, 'request')).message).toBe(CHANGED)
      await $.command.run(run('captains-bridge'))
      expect(await stateOf($, 'binding')).toEqual({ sessionId: 'sess-new' })
    })
  }

  test('session.end stays ended through a plugin reload', WITH_PROBE, async ($, on) => {
    host(on, { id: 'sess-a' })
    await $.classic.SessionStart({ source: 'startup', session_id: 'sess-a' })
    await $.session.end({ reason: 'clear', sessionId: 'sess-a', resume: { id: 'sess-a' } })
    await $.session.start(START)
    expect(await stateOf($, 'binding')).toBe(null)
    expect((await stateOf($, 'request')).message).toBe(CHANGED)
  })

  test('passes the event on unchanged', async ($, on) => {
    const seen = host(on)
    await $.classic.SessionStart({ source: 'startup', session_id: 'sess-a' })
    expect(seen.reached.length).toBe(1)
    expect(seen.reached[0].source).toBe('startup')
    expect(seen.reached[0].session_id).toBe('sess-a')
  })
})

describe('session.end', () => {
  test('ends the binding and shows the changed state', WITH_PROBE, async ($, on) => {
    host(on)
    await $.classic.SessionStart({ source: 'startup', session_id: 'sess-a' })
    await $.session.end({ reason: 'clear', sessionId: 'sess-a', resume: { id: 'sess-a' } })
    expect(await stateOf($, 'binding')).toBe(null)
    expect((await stateOf($, 'request')).message).toBe(CHANGED)
  })
})

describe('/captains-bridge', () => {
  test('opens the pane bound to the session and starts no turn', WITH_PROBE, async ($, on) => {
    const seen = host(on, { id: 'sess-cmd' })
    const out = await $.command.run(run('captains-bridge', '', 60))
    expect(seen.opened).toEqual([{ id: 'captains-bridge', title: "Captain's Bridge" }])
    expect(seen.turns).toBe(0)
    expect(out.text).toContain("Captain's Bridge")
    expect(await stateOf($, 'binding')).toEqual({ sessionId: 'sess-cmd' })
  })

  test('opens at a narrow terminal width', async ($, on) => {
    const seen = host(on)
    await $.command.run(run('captains-bridge', '', 40))
    expect(seen.opened.length).toBe(1)
  })

  test('keeps the established binding and its transcript path', WITH_PROBE, async ($, on) => {
    host(on, { id: 'sess-a' })
    await $.classic.SessionStart({
      source: 'startup',
      session_id: 'sess-a',
      transcript_path: '/synthetic/transcripts/sess-a.jsonl',
    })
    await $.command.run(run('captains-bridge'))
    expect((await stateOf($, 'binding')).transcriptPath).toBe(
      '/synthetic/transcripts/sess-a.jsonl',
    )
  })

  test('returns a text status where nothing draws', async ($, on) => {
    host(on, { isPlaced: false })
    const out = await $.command.run(run('captains-bridge'))
    expect(out.text).toContain('No walkthrough has been prepared')
  })

  test('a too-old host names the required version and does not bind', WITH_PROBE, async ($, on) => {
    host(on, { version: '2.1.292' })
    const out = await $.command.run(run('captains-bridge'))
    expect(out.text).toContain('2.1.293')
    expect(out.text).toContain('2.1.292')
    expect(await stateOf($, 'binding')).toBe(null)
  })

  test('after a conversation change, opening binds the new conversation', WITH_PROBE, async ($, on) => {
    host(on, { id: 'sess-new' })
    await $.classic.SessionStart({ source: 'startup', session_id: 'sess-old' })
    await $.classic.SessionStart({ source: 'clear', session_id: 'sess-new' })
    expect(await stateOf($, 'binding')).toBe(null)
    await $.command.run(run('captains-bridge'))
    expect(await stateOf($, 'binding')).toEqual({ sessionId: 'sess-new' })
    expect((await stateOf($, 'request')).message).toBe(undefined)
  })
})

describe('the text overview', () => {
  const body = {
    objective: 'Ship the synthetic feature',
    summary: 'One item merged; one open.',
    evidence: ['aaaaaaaa'],
    items: [
      { id: '1', title: 'Add widget', group: 'changed', status: 'Merged', summary: 's', detail: 'd', evidence: ['aaaaaaaa'] },
      { id: '2', title: 'Fix gadget', group: 'unresolved', status: 'Open', summary: 's', detail: 'd', evidence: ['aaaaaaaa'] },
    ],
  } as const

  test('lists objective, summary, group labels and item statuses', () => {
    const text = overviewText(body as any, null)
    expect(text).toContain('Objective: Ship the synthetic feature')
    expect(text).toContain('Summary: One item merged; one open.')
    expect(text).toContain('What changed:')
    expect(text).toContain('- Add widget (Merged)')
    expect(text).toContain('What remains unresolved:')
    expect(text).toContain('- Fix gadget (Open)')
    expect(text).not.toContain('Other recorded activity')
  })

  test('is a short truthful status before a walkthrough exists', () => {
    expect(overviewText(null, null)).toBe(
      'No walkthrough has been prepared for this conversation yet.',
    )
    expect(overviewText(null, { recordsIncluded: 3, recordsTotal: 5 })).toContain(
      '3 of 5 records are read',
    )
  })
})

describe('the drawn pane', () => {
  for (const surface of SURFACES) {
    test(`shows the bound conversation on ${surface}`, async ($, on) => {
      host(on, { id: 'sess-draw' })
      await $.command.run(run('captains-bridge'))
      const pane = await $.ui.mount({ ...PANE, surface })
      expect(await pane.find({ type: 'Text', text: "Captain's Bridge" })).not.toBe(undefined)
      expect(await pane.find({ type: 'Text', text: 'sess-draw' })).not.toBe(undefined)
      expect(
        await pane.find({ type: 'Text', text: 'No walkthrough has been prepared' }),
      ).not.toBe(undefined)
      await pane.unmount()
    })

    test(`shows the changed-conversation state on ${surface}`, async ($, on) => {
      host(on)
      await $.classic.SessionStart({ source: 'startup', session_id: 'sess-a' })
      await $.classic.SessionStart({ source: 'resume', session_id: 'sess-b' })
      const pane = await $.ui.mount({ ...PANE, surface })
      expect(await pane.find({ type: 'Text', text: CHANGED })).not.toBe(undefined)
      await pane.unmount()
    })

    test(`keeps the changed-conversation state on ${surface} after a reload`, async ($, on) => {
      host(on, { id: 'sess-b' })
      await $.classic.SessionStart({ source: 'startup', session_id: 'sess-a' })
      await $.classic.SessionStart({ source: 'clear', session_id: 'sess-b' })
      await $.session.start(START)
      const pane = await $.ui.mount({ ...PANE, surface })
      expect(await pane.find({ type: 'Text', text: CHANGED })).not.toBe(undefined)
      await pane.unmount()
    })

    test(`names the required version on ${surface} when the host is too old`, async ($, on) => {
      host(on, { version: '2.1.200' })
      const pane = await $.ui.mount({
        ...PANE,
        surface,
        props: { ...PANE.props, bodyColumns: 40, placement: 'inline' },
      })
      expect(await pane.find({ type: 'Text', text: '2.1.293' })).not.toBe(undefined)
      await pane.unmount()
    })
  }
})

describe('no hidden work', () => {
  test('sends nothing and arms no timer on start or command', WITH_PROBE, async ($, on) => {
    const seen = host(on)
    await $.session.start(START)
    await $.command.run(run('captains-bridge'))
    expect(seen.turns).toBe(0)
    expect(seen.timers).toBe(0)
  })
})
