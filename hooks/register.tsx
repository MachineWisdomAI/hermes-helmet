// Captain's Bridge: the Claude Code mod of the Hermes Helmet plugin.
//
// This slice is the skeleton: the `/captains-bridge` command, the exact
// session binding, the changed-conversation state, the minimum-version gate
// and a truthful pane and text overview. It reads nothing, sends no message,
// starts no turn and runs no timer. Later slices add the reader, the
// explanation and the actions behind the same state contract (../types).
//
// The calls this module makes are checked in CI against the read-only
// allowlist from issue #55 (scripts/check_mod_calls.py).
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'
import type { Walkthrough } from '../types'

export const PANE_ID = 'captains-bridge'
export const PANE_TITLE = "Captain's Bridge"
export const COMMAND = 'captains-bridge'
// The lowest Claude Code build this release is verified on (see
// docs/first-officer-plugins.md). It is never lower than 2.1.293.
export const MIN_VERSION = '2.1.293'
export const CHANGED_MESSAGE =
  'This conversation changed. Run /captains-bridge to open the Bridge for it.'

const binding = atom({ plugin: 'hermes-helmet', key: 'binding' } as const, null)
const records = atom({ plugin: 'hermes-helmet', key: 'records' } as const, null)
const walkthrough = atom(
  { plugin: 'hermes-helmet', key: 'walkthrough' } as const,
  null,
)
const view = atom({ plugin: 'hermes-helmet', key: 'view' } as const, { stack: [] })
const pending = atom({ plugin: 'hermes-helmet', key: 'pending' } as const, null)
const request = atom(
  { plugin: 'hermes-helmet', key: 'request' } as const,
  { generation: 0, status: 'idle', retain: false },
)

// Sources of a classic SessionStart that start a different conversation.
const CHANGING_SOURCES = ['clear', 'resume', 'fork']

export function parseVersion(text: string): number[] | null {
  const match = /^(\d+)\.(\d+)\.(\d+)/.exec(text.trim())
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null
}

export function isSupported(version: string): boolean {
  const have = parseVersion(version)
  const need = parseVersion(MIN_VERSION)
  if (have === null || need === null) return false
  for (let i = 0; i < 3; i++) {
    if (have[i] !== need[i]) return have[i] > need[i]
  }
  return true
}

export function tooOldMessage(version: string): string {
  return (
    `Captain's Bridge needs Claude Code ${MIN_VERSION} or newer; this is ` +
    `${version || 'an unknown version'}. Update Claude Code, then run ` +
    '/captains-bridge again.'
  )
}

const GROUPS: Array<[Walkthrough['items'][number]['group'], string]> = [
  ['changed', 'What changed'],
  ['unresolved', 'What remains unresolved'],
  ['activity', 'Other recorded activity'],
]

export function overviewText(
  body: Walkthrough | null,
  counts: { recordsIncluded: number; recordsTotal: number } | null,
): string {
  if (body === null) {
    const known =
      counts === null
        ? ''
        : ` ${counts.recordsIncluded} of ${counts.recordsTotal} records are read.`
    return `No walkthrough has been prepared for this conversation yet.${known}`
  }
  const lines = [`Objective: ${body.objective}`, `Summary: ${body.summary}`]
  for (const [group, label] of GROUPS) {
    const items = body.items.filter(item => item.group === group)
    if (items.length === 0) continue
    lines.push(`${label}:`)
    for (const item of items) lines.push(`- ${item.title} (${item.status})`)
  }
  return lines.join('\n')
}

// ---------------------------------------------------------------------------
// Show Me and Retro. They run the Captain's own installed commands in this
// exact session through $.command.run, which the host queues until the first
// officer is idle. Nothing is bundled, no prompt is submitted or filled, no
// subagent starts and nothing goes to another session.

export type Action = 'show-me' | 'retro'
export type ActionNames = { 'show-me': string; retro: string }
export type Availability = Record<Action, string | null>

export const ACTION_LABELS: Record<Action, string> = { 'show-me': 'Show Me', retro: 'Retro' }
export const DEFAULT_NAMES: ActionNames = { 'show-me': 'show-me', retro: 'retro' }

export function missingMessage(action: Action, configured: string): string {
  const key = action === 'show-me' ? 'showMeCommand' : 'retroCommand'
  return `${ACTION_LABELS[action]} isn't installed. Install a skill named ${configured}, or set ${key}.`
}

export function actionNames(options: unknown): ActionNames {
  const bag = (options ?? {}) as Record<string, unknown>
  const pick = (value: unknown, fallback: string): string =>
    typeof value === 'string' && value.trim() !== '' ? value.trim().replace(/^\//, '') : fallback
  return {
    'show-me': pick(bag.showMeCommand, DEFAULT_NAMES['show-me']),
    retro: pick(bag.retroCommand, DEFAULT_NAMES.retro),
  }
}

// The names $.command.list() reports, whatever the entry shape.
export function listedNames(listed: unknown): string[] {
  const raw = Array.isArray(listed)
    ? listed
    : listed !== null && typeof listed === 'object' && Array.isArray((listed as any).commands)
      ? (listed as any).commands
      : []
  const names: string[] = []
  for (const entry of raw) {
    const name = typeof entry === 'string' ? entry : (entry as { name?: unknown } | null)?.name
    if (typeof name === 'string' && name !== '') names.push(name.replace(/^\//, ''))
  }
  return names
}

// A configured name matches itself, or `<plugin>:<name>` for an installed plugin.
export function resolveCommand(configured: string, names: string[]): string | null {
  if (names.includes(configured)) return configured
  return names.find(name => name.endsWith(`:${configured}`)) ?? null
}

export function availabilityOf(listed: unknown, names: ActionNames): Availability {
  const found = listedNames(listed)
  return {
    'show-me': resolveCommand(names['show-me'], found),
    retro: resolveCommand(names.retro, found),
  }
}

// Commands the host refused as unknown since the last discovery.
const unknown = new Set<Action>()
let availability: Availability | null = null

// Called when the Bridge opens and on every refresh.
export async function discoverCommands($: any, names: ActionNames): Promise<Availability> {
  unknown.clear()
  let listed: unknown = []
  try {
    listed = await $.command.list()
  } catch {
    listed = []
  }
  availability = availabilityOf(listed, names)
  return availability
}

export function commandFor(action: Action): string | null {
  return unknown.has(action) || availability === null ? null : availability[action]
}

type ScopeItem = { title: string; evidence: string[] }

// "this session", or the selected item's title with its evidence refs and the
// timestamps of the records it cites.
export function scopeArgs(
  item: ScopeItem | null,
  records: Array<{ ref: string; timestamp: string }> | null,
): string {
  if (item === null) return 'this session'
  const at = new Map((records ?? []).map(r => [r.ref, r.timestamp]))
  const cited = item.evidence.map(ref => {
    const when = at.get(ref)
    return when === undefined ? ref : `${ref} (${when})`
  })
  return cited.length === 0
    ? `${item.title}`
    : `${item.title}; evidence: ${cited.join(', ')}`
}

export async function selectedScope($: any): Promise<string> {
  const nav = await read($, view)
  const top = nav.stack[nav.stack.length - 1] as { itemId?: string } | undefined
  const saved = await read($, walkthrough)
  if (top?.itemId === undefined || saved === null) return 'this session'
  const item = saved.body.items.find(candidate => candidate.id === top.itemId)
  if (item === undefined) return 'this session'
  const summary = await read($, records)
  return scopeArgs(item, summary === null ? null : summary.summary.records)
}

const notices: Partial<Record<Action, string>> = {}

// One press: claim `pending` atomically (repeat presses are ignored), queue
// exactly one command.run, and clear on settlement whatever happened.
export async function runAction($: any, action: Action, configured: string): Promise<void> {
  const command = commandFor(action)
  if (command === null) return
  let claimed = false
  await update($, pending, current => {
    claimed = current === null
    return current === null ? { action, since: new Date().toISOString() } : current
  })
  if (!claimed) return
  delete notices[action]
  try {
    const args = await selectedScope($)
    await $.command.run({ command, args })
  } catch (error) {
    // A rejection naming an unknown command, or a command that is no longer
    // listed, is the missing state. Any other rejection is a retryable notice.
    const text = error instanceof Error ? error.message : String(error)
    let stillListed = true
    try {
      stillListed = resolveCommand(configured, listedNames(await $.command.list())) !== null
    } catch {
      stillListed = true
    }
    if (!stillListed || /no command named|unknown command/i.test(text)) {
      unknown.add(action)
    } else {
      notices[action] = `${ACTION_LABELS[action]} couldn't be queued. Try again.`
    }
  } finally {
    await update($, pending, () => null)
  }
}

export function actionBlock(
  ctx: { Box: any; Text: any; Button: any },
  action: Action,
  configured: string,
  isPending: boolean,
  press: () => Promise<void>,
): any {
  const { Box, Text, Button } = ctx
  const label = ACTION_LABELS[action]
  const command = commandFor(action)
  if (command === null) {
    return (
      <Box key={`${action}-box`} flexDirection="column">
        <Text key={action} dimColor wrap="wrap">
          {label}: {missingMessage(action, configured)}
        </Text>
      </Box>
    )
  }
  return (
    <Box key={`${action}-box`} flexDirection="column">
      <Button key={action} onPress={press}>
        {isPending ? `${label} (queued)` : label}
      </Button>
      {notices[action] === undefined ? null : (
        <Text key={`${action}-notice`} color="warning" wrap="wrap">
          {notices[action]}
        </Text>
      )}
    </Box>
  )
}

// Whether this session draws panes at all. Set at session.start from the
// surface the host reports; unknown (null) is decided by what ui.open says.
let drawsPanes = true

export const register: Register = (on, options) => {
  const names = actionNames(options)
  on('session.start', async ($, e, next) => {
    drawsPanes = e.isInteractive !== false && e.surface !== 'vscode'
    await $.command.register({
      name: COMMAND,
      description: "Open the Captain's Bridge for this conversation",
      immediate: true,
    })
    // Retain the exact session ID on start and on reload. An established
    // binding for this same session stays; a different one has ended.
    // A binding ended by clear/resume/fork/end stays ended through a reload
    // until an explicit /captains-bridge command (which clears the message).
    const id = await $.session.id()
    const ended = (await read($, request)).message === CHANGED_MESSAGE
    await update($, binding, current => {
      if (current === null) return ended ? null : { sessionId: id }
      return current.sessionId === id ? current : null
    })
    return next(e)
  })

  on('classic.SessionStart', async ($, e, next) => {
    if (CHANGING_SOURCES.includes(e.source)) {
      await update($, binding, () => null)
      await update($, request, current => ({
        generation: current.generation + 1,
        status: 'idle',
        retain: false,
        message: CHANGED_MESSAGE,
      }))
    } else {
      const path = e.transcript_path
      await update($, binding, () => ({
        sessionId: e.session_id,
        ...(path ? { transcriptPath: path } : {}),
      }))
    }
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    await update($, binding, () => null)
    await update($, request, current => ({
      generation: current.generation + 1,
      status: 'idle',
      retain: false,
      message: CHANGED_MESSAGE,
    }))
    return next(e)
  })

  on('command.run', { command: COMMAND }, async $ => {
    const { version } = await $.session.version()
    const id = await $.session.id()
    if (isSupported(version)) {
      // Explicit opening binds to this exact session; an established binding
      // for it is kept (it carries the transcript path).
      await update($, binding, current =>
        current !== null && current.sessionId === id ? current : { sessionId: id },
      )
      await update($, request, current =>
        current.message === CHANGED_MESSAGE
          ? { ...current, status: 'idle', message: undefined }
          : current,
      )
    }
    if (isSupported(version)) await discoverCommands($, names)
    const opened = await $.ui.open({ id: PANE_ID, title: PANE_TITLE })
    if (!isSupported(version)) return { text: tooOldMessage(version) }
    if (drawsPanes && opened.isPlaced) {
      return { text: `${PANE_TITLE} is open for this conversation.` }
    }
    const state = await $.state.get({ plugin: 'hermes-helmet', key: 'walkthrough' })
    const summary = await $.state.get({ plugin: 'hermes-helmet', key: 'records' })
    const body = (state.value as { body: Walkthrough } | null | undefined)?.body ?? null
    const coverage =
      (summary.value as { coverage: { recordsIncluded: number; recordsTotal: number } } | null | undefined)
        ?.coverage ?? null
    return { text: overviewText(body, coverage) }
  })

  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const { version } = await $.session.version()
    const bound = await read($, binding)
    const status = await read($, request)
    const saved = await read($, walkthrough)
    const counts = await read($, records)
    const busy = await read($, pending)

    if (!isSupported(version)) {
      return (
        <Box flexDirection="column">
          <Text bold>{PANE_TITLE}</Text>
          <Text wrap="wrap">{tooOldMessage(version)}</Text>
        </Box>
      )
    }
    if (bound === null) {
      return (
        <Box flexDirection="column">
          <Text bold>{PANE_TITLE}</Text>
          <Text wrap="wrap">
            {status.message ?? 'Run /captains-bridge to open the Bridge for this conversation.'}
          </Text>
        </Box>
      )
    }
    const body = saved === null ? null : saved.body
    if (availability === null) await discoverCommands($, names)
    const ctx = { Box, Text, Button }
    return (
      <Box flexDirection="column">
        <Text bold>{PANE_TITLE}</Text>
        <Text dimColor wrap="wrap">
          Conversation {bound.sessionId}
        </Text>
        {overviewText(body, counts === null ? null : counts.coverage)
          .split('\n')
          .map(line => (
            <Text wrap="wrap">{line}</Text>
          ))}
        <Box key="actions" flexDirection="column" marginTop={1}>
          {(['show-me', 'retro'] as Action[]).map(action =>
            actionBlock(
              ctx,
              action,
              names[action],
              busy !== null && busy.action === action,
              () => runAction($, action, names[action]),
            ),
          )}
        </Box>
      </Box>
    )
  })
}
