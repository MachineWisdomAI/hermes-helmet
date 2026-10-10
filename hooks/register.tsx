// Captain's Bridge: the Claude Code mod of the Hermes Helmet plugin.
//
// It reads the session's saved records with `helmet bridge read`, draws the
// Changes First walkthrough (overview, full item detail, Back with its place
// restored, Refresh records), and prepares a new explanation in the background
// with one tool-less model request. The request never starts a turn, submits a
// prompt, calls a tool, spawns a subagent, waits or polls; the first officer's
// task is never interrupted. State follows the contract in ../types.
//
// The calls this module makes are checked in CI against the read-only
// allowlist from issue #55 (scripts/check_mod_calls.py).
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'
import type {
  BridgeState,
  Coverage,
  RecordsSummary,
  RequestStatus,
  SessionRecord,
  Walkthrough,
  WalkthroughItem,
} from '../types'

type BridgeWalkthrough = NonNullable<BridgeState['walkthrough']>

export const PANE_ID = 'captains-bridge'
export const PANE_TITLE = "Captain's Bridge"
export const COMMAND = 'captains-bridge'
// The lowest Claude Code build this release is verified on (see
// docs/first-officer-plugins.md). It is never lower than 2.1.293.
export const MIN_VERSION = '2.1.293'
export const CHANGED_MESSAGE =
  'This conversation changed. Run /captains-bridge to open the Bridge for it.'
export const READER_SCHEMA_MAJOR = 1
export const READER_TIMEOUT_MS = 30000
// Subdued, distinct backgrounds; the text labels carry the meaning too.
export const REPORT_BACKGROUND = '#2f3b4c'
export const DISPOSITION_BACKGROUND = '#3b3a2f'

// One bounded, tool-less explanation request. The instructions and the schema
// go first and are cache-marked; the record summary is the last block.
export const MAX_TOKENS = 16000
export const DIFFICULT_MESSAGE = "The explanation didn't match the records"
export const EMPTY_MESSAGE = 'The explanation came back empty'
export const TIMEOUT_MESSAGE = 'The explanation request hit its time limit'
export const RELOAD_MESSAGE = 'Interrupted by a plugin reload'

export const EXPLANATION_INSTRUCTIONS = [
  'You explain one Claude Code session to its Captain from the records you are given.',
  'Answer with one JSON object and nothing else: no prose, no code fences.',
  '',
  'Rules:',
  '- Make only claims the cited records support. Every conclusion, step, comparison and link cites record refs from this request.',
  '- Keep the record text and your interpretation distinct. Never restate a record as if it were your own conclusion.',
  '- Keep a reported completion separate from its acceptance. A finished turn, a successful command or a green run is not delivery or acceptance.',
  '- Assign actors by actual responsibility: Captain, First officer, Hermes, or Other agent. Use Hermes only where a cited record attributes the work to the Hermes worker.',
  '- Never treat a quiet interval or a gap as a stall.',
  '- Never invent a before state, a diff, a link, a parent-child execution tree, or runtime success.',
  '- Use readable, descriptive titles. Never use planning codes, task ids or raw UUIDs as names.',
  '- Say where coverage is incomplete, at the affected item.',
  '',
  'The walkthrough shape is given as a JSON schema; the records follow as JSON.',
].join('\n')

export const WALKTHROUGH_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['objective', 'summary', 'evidence', 'items'],
  properties: {
    objective: { type: 'string' },
    summary: { type: 'string' },
    evidence: { type: 'array', minItems: 1, items: { type: 'string' } },
    items: {
      type: 'array',
      minItems: 1,
      maxItems: 24,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'title', 'group', 'status', 'summary', 'detail', 'evidence'],
        properties: {
          id: { type: 'string' },
          title: { type: 'string' },
          group: { enum: ['changed', 'unresolved', 'activity'] },
          status: { type: 'string' },
          summary: { type: 'string' },
          detail: { type: 'string' },
          evidence: { type: 'array', minItems: 1, items: { type: 'string' } },
          steps: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              required: ['actor', 'label', 'detail', 'evidence'],
              properties: {
                actor: { enum: ['Captain', 'First officer', 'Hermes', 'Other agent'] },
                label: { type: 'string' },
                detail: { type: 'string' },
                evidence: { type: 'array', minItems: 1, items: { type: 'string' } },
              },
            },
          },
          reported: { $ref: '#/$defs/note' },
          disposition: { $ref: '#/$defs/note' },
          change: {
            type: 'object',
            additionalProperties: false,
            required: ['before', 'after', 'explanation', 'evidence'],
            properties: {
              before: { type: 'string' },
              after: { type: 'string' },
              explanation: { type: 'string' },
              evidence: { type: 'array', minItems: 1, items: { type: 'string' } },
            },
          },
          links: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              required: ['label', 'url', 'evidence'],
              properties: {
                label: { type: 'string' },
                url: { type: 'string' },
                evidence: { type: 'array', minItems: 1, items: { type: 'string' } },
              },
            },
          },
        },
      },
    },
  },
  $defs: {
    note: {
      type: 'object',
      additionalProperties: false,
      required: ['label', 'actor', 'evidence'],
      properties: {
        label: { type: 'string' },
        actor: { enum: ['Captain', 'First officer', 'Hermes', 'Other agent'] },
        evidence: { type: 'array', minItems: 1, items: { type: 'string' } },
      },
    },
  },
}

export type ExplanationOptions = { model: string; timeoutMs: number }

export function explanationOptions(options: unknown): ExplanationOptions {
  const bag = (options ?? {}) as Record<string, unknown>
  const model =
    typeof bag.explanationModel === 'string' && bag.explanationModel.trim() !== ''
      ? bag.explanationModel.trim()
      : 'sonnet'
  const seconds =
    typeof bag.explanationTimeoutSeconds === 'number' &&
    Number.isFinite(bag.explanationTimeoutSeconds) &&
    bag.explanationTimeoutSeconds > 0
      ? bag.explanationTimeoutSeconds
      : 180
  return { model, timeoutMs: Math.round(seconds * 1000) }
}

const binding = atom({ plugin: 'hermes-helmet', key: 'binding' } as const, null)
const records = atom({ plugin: 'hermes-helmet', key: 'records' } as const, null)
const walkthrough = atom(
  { plugin: 'hermes-helmet', key: 'walkthrough' } as const,
  null,
)
const request = atom(
  { plugin: 'hermes-helmet', key: 'request' } as const,
  { generation: 0, status: 'idle', retain: false },
)
const view = atom({ plugin: 'hermes-helmet', key: 'view' } as const, { stack: [] })
const pending = atom({ plugin: 'hermes-helmet', key: 'pending' } as const, null)

// One AbortController per live request, in module scope. A plugin reload
// re-evaluates this module, so the controller is gone while `request.status`
// stays `preparing` in state: that is the reload case below.
let liveRequest: { generation: number; controller: AbortController } | null = null

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

const GROUPS: Array<[WalkthroughItem['group'], string]> = [
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
// The record reader: `helmet bridge read`, run as a child process.

export type ReaderOutcome =
  | { ok: true; summary: RecordsSummary }
  | { ok: false; text: string }

export function readerArgv(
  helmetCommand: string,
  bound: { sessionId: string; transcriptPath?: string },
): string[] {
  const argv = [helmetCommand, 'bridge', 'read', '--session', bound.sessionId]
  // An unknown transcript path leaves the lookup to the reader, by exact ID.
  if (bound.transcriptPath) argv.push('--transcript', bound.transcriptPath)
  return argv
}

function schemaMajor(schema: unknown): number | null {
  const match =
    typeof schema === 'string' ? /^hermes-helmet\.bridge\.records\/(\d+)$/.exec(schema) : null
  return match ? Number(match[1]) : null
}

const FIXES: Record<string, string> = {
  'session-not-found':
    'No saved record file was found for this exact conversation. Run /captains-bridge again from the conversation you want, or check that Claude Code saves sessions.',
  'ambiguous-session':
    'More than one saved record file carries this conversation ID, and the Bridge never chooses one. Remove the duplicate or open the Bridge from a conversation with a single record file.',
  'session-mismatch':
    'The saved records belong to a different conversation than the one this Bridge is bound to. Run /captains-bridge again in the conversation you want.',
  'unsupported-format':
    'These saved records are in a format this release does not support. Update the hermes-helmet package and plugin together, then refresh.',
  unreadable:
    'The saved records could not be read. Check the file permissions and that the disk is available, then refresh.',
}

function firstLine(text: string): string {
  const line = text.split('\n').find(part => part.trim() !== '') ?? ''
  return line.trim().slice(0, 240)
}

function validSummary(value: any): value is RecordsSummary {
  const c = value?.coverage
  return (
    value !== null &&
    typeof value === 'object' &&
    typeof value.sessionId === 'string' &&
    typeof value.readAt === 'string' &&
    typeof value.fingerprint === 'string' &&
    Array.isArray(value.records) &&
    value.records.every(
      (r: any) =>
        r !== null &&
        typeof r === 'object' &&
        typeof r.ref === 'string' &&
        typeof r.timestamp === 'string' &&
        typeof r.text === 'string',
    ) &&
    c !== null &&
    typeof c === 'object' &&
    typeof c.recordsTotal === 'number' &&
    typeof c.recordsIncluded === 'number' &&
    typeof c.bytesOmitted === 'number' &&
    typeof c.unsupportedSkipped === 'number' &&
    typeof c.pendingTail === 'boolean' &&
    Array.isArray(value.warnings) &&
    value.warnings.every(
      (w: any) =>
        typeof w === 'string' ||
        (w !== null && typeof w === 'object' && typeof w.message === 'string'),
    )
  )
}

export async function runReader(
  $: any,
  helmetCommand: string,
  bound: { sessionId: string; transcriptPath?: string },
): Promise<ReaderOutcome> {
  const shown = `${helmetCommand} bridge read`
  let result: any
  try {
    result = await $.process.run(readerArgv(helmetCommand, bound), {
      timeoutMs: READER_TIMEOUT_MS,
    })
  } catch (error) {
    const reason = firstLine(String((error as { message?: unknown })?.message ?? error))
    return {
      ok: false,
      text:
        `Could not run \`${shown}\`${reason ? ` (${reason})` : ''}. ` +
        'Install the helmet command so Claude Code can reach it, or set helmetCommand ' +
        'in the plugin settings to its absolute path (Claude Desktop may not inherit your shell PATH). ' +
        'The reader also stops after 30 seconds.',
    }
  }
  if (result === null || typeof result !== 'object') {
    return {
      ok: false,
      text: `\`${shown}\` returned nothing the Bridge can use. Check that helmetCommand names the helmet command.`,
    }
  }
  const exitCode = typeof result.exitCode === 'number' ? result.exitCode : 1
  const stdout = typeof result.stdout === 'string' ? result.stdout : ''
  const stderr = typeof result.stderr === 'string' ? result.stderr : ''
  let parsed: any = null
  if (!result.isStdoutTruncated) {
    try {
      parsed = JSON.parse(stdout)
    } catch {
      parsed = null
    }
  }
  const hasError =
    parsed !== null &&
    typeof parsed === 'object' &&
    parsed.error !== null &&
    typeof parsed.error === 'object' &&
    typeof parsed.error.code === 'string'
  if (parsed !== null && typeof parsed === 'object' && 'schema' in parsed) {
    if (schemaMajor(parsed.schema) !== READER_SCHEMA_MAJOR) {
      return {
        ok: false,
        text:
          `\`${shown}\` answered with schema ${JSON.stringify(parsed.schema)}, but this plugin reads ` +
          `hermes-helmet.bridge.records/${READER_SCHEMA_MAJOR}. Update the hermes-helmet package and the plugin ` +
          'to matching releases, then refresh.',
      }
    }
  }
  if (hasError) {
    const code: string = parsed.error.code
    const message =
      typeof parsed.error.message === 'string' ? parsed.error.message : 'no message'
    const fix = FIXES[code] ?? 'Update the hermes-helmet package and plugin together, then refresh.'
    return { ok: false, text: `The reader reported ${code}: ${message} ${fix}` }
  }
  if (exitCode !== 0) {
    const tail = firstLine(stderr) || firstLine(stdout)
    const old = /invalid choice|unrecognized arguments|no such command|unknown command|usage:/i.test(
      `${stderr}\n${stdout}`,
    )
    return {
      ok: false,
      text:
        `\`${shown}\` exited with code ${exitCode} without a reader error` +
        `${tail ? ` (${tail})` : ''}. ` +
        (old
          ? 'This helmet is too old to read sessions: install hermes-helmet 0.7.0 or newer, or set helmetCommand to a newer one.'
          : 'Check that helmetCommand names a current helmet command, then refresh.'),
    }
  }
  if (result.isStdoutTruncated || parsed === null || typeof parsed !== 'object') {
    return {
      ok: false,
      text:
        `\`${shown}\` printed output the Bridge cannot parse` +
        `${result.isStdoutTruncated ? ' (it was cut at 4 MiB)' : ''}. ` +
        'Check that helmetCommand names the helmet command and that it is current.',
    }
  }
  if (schemaMajor(parsed.schema) !== READER_SCHEMA_MAJOR) {
    return {
      ok: false,
      text:
        `\`${shown}\` answered with ${parsed.schema === undefined ? 'no schema' : `schema ${JSON.stringify(parsed.schema)}`}, ` +
        `but this plugin reads hermes-helmet.bridge.records/${READER_SCHEMA_MAJOR}. ` +
        'Update the hermes-helmet package and the plugin to matching releases, then refresh. The records already shown were kept.',
    }
  }
  if (!validSummary(parsed)) {
    return {
      ok: false,
      text: `\`${shown}\` answered in a shape this plugin does not understand (a record, the coverage counts or a warning is malformed). Update the hermes-helmet package and plugin together, then refresh. The records already shown were kept.`,
    }
  }
  if (parsed.sessionId !== bound.sessionId) {
    return {
      ok: false,
      text:
        `The reader answered for a different conversation (${parsed.sessionId}) than the one this Bridge is bound to (${bound.sessionId}). ` +
        'Nothing was adopted and the Bridge did not rebind. Run /captains-bridge again in the conversation you want.',
    }
  }
  return { ok: true, summary: parsed }
}

function coverageOf(summary: RecordsSummary): Coverage {
  return summary.coverage
}

// Rereads only the saved records. It never touches the walkthrough, the view
// stack or the selection, and it calls no model and sends nothing.
let reading = false
export async function refreshRecords($: any, helmetCommand: string): Promise<void> {
  if (reading) return
  reading = true
  try {
    const bound = await read($, binding)
    if (bound === null) return
    const outcome = await runReader($, helmetCommand, bound)
    // A conversation change while the reader ran: nothing is adopted.
    const now = await read($, binding)
    if (now === null || now.sessionId !== bound.sessionId) return
    if (!outcome.ok) {
      await update($, view, current => ({
        ...current,
        notice: { kind: 'error', text: outcome.text },
      }))
      return
    }
    const summary = outcome.summary
    await update($, records, () => ({
      fingerprint: summary.fingerprint,
      readAt: summary.readAt,
      coverage: coverageOf(summary),
      summary,
    }))
    await update($, view, current => {
      const { notice: _gone, ...rest } = current as any
      return rest
    })
  } finally {
    reading = false
  }
}

// ---------------------------------------------------------------------------
// Preparing an explanation. One bounded, tool-less model request over the
// request's own immutable record snapshot. Nothing here submits a prompt,
// starts a turn, calls a tool, spawns a subagent, waits, polls or retries.

type RequestSnapshot = {
  readAt: string
  fingerprint: string
  summary: RecordsSummary
}

// The source-bound validation ported from the approved spike: source is kept
// separate from interpretation, support is required, and a link must appear
// verbatim in a record the link itself cites.
export function validateWalkthrough(value: any, summary: RecordsSummary): string | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return 'shape'
  const seen = new Set(summary.records.map(rec => rec.ref))
  const textOf = new Map(summary.records.map(rec => [rec.ref, rec.text]))
  const prose = (v: unknown): boolean => typeof v === 'string' && v.trim() !== ''
  const ACTORS = ['Captain', 'First officer', 'Hermes', 'Other agent']
  const evidence = (v: unknown): string[] | null => {
    if (!Array.isArray(v) || v.length === 0) return null
    const refs: string[] = []
    for (const ref of v) {
      if (typeof ref !== 'string' || !seen.has(ref)) return null
      refs.push(ref)
    }
    return refs
  }
  if (!prose(value.objective) || !prose(value.summary)) return 'prose'
  if (evidence(value.evidence) === null) return 'evidence'
  if (!Array.isArray(value.items) || value.items.length === 0) return 'items'
  const ids = new Set<string>()
  for (const item of value.items) {
    if (item === null || typeof item !== 'object' || Array.isArray(item)) return 'item'
    if (
      !prose(item.id) ||
      !prose(item.title) ||
      !prose(item.status) ||
      !prose(item.summary) ||
      !prose(item.detail)
    ) {
      return 'item-text'
    }
    if (!['changed', 'unresolved', 'activity'].includes(item.group)) return 'group'
    if (ids.has(item.id)) return 'duplicate-id'
    ids.add(item.id)
    if (evidence(item.evidence) === null) return 'item-evidence'
    if (item.steps !== undefined) {
      if (!Array.isArray(item.steps)) return 'steps'
      for (const step of item.steps) {
        if (step === null || typeof step !== 'object') return 'step'
        if (!prose(step.label) || !prose(step.detail)) return 'step-text'
        if (!ACTORS.includes(step.actor)) return 'actor'
        if (evidence(step.evidence) === null) return 'step-evidence'
      }
    }
    for (const field of ['reported', 'disposition'] as const) {
      const note = item[field]
      if (note === undefined || note === null) continue
      if (typeof note !== 'object') return field
      if (!prose(note.label) || !ACTORS.includes(note.actor)) return field
      if (evidence(note.evidence) === null) return `${field}-evidence`
    }
    if (item.change !== undefined && item.change !== null) {
      const change = item.change
      if (typeof change !== 'object') return 'change'
      if (!prose(change.before) || !prose(change.after) || !prose(change.explanation)) {
        return 'change-text'
      }
      if (evidence(change.evidence) === null) return 'change-evidence'
    }
    if (item.links !== undefined) {
      if (!Array.isArray(item.links)) return 'links'
      for (const link of item.links) {
        if (link === null || typeof link !== 'object') return 'link'
        if (!prose(link.label) || typeof link.url !== 'string') return 'link'
        const source = evidence(link.evidence)
        if (source === null) return 'link-evidence'
        // The address must appear verbatim in a record the link cites.
        if (!source.some(ref => (textOf.get(ref) ?? '').includes(link.url))) return 'link-source'
      }
    }
  }
  return null
}

export function statusLine(request: BridgeState['request']): string {
  switch (request.status) {
    case 'preparing':
      return 'Preparing a new explanation in the background. The first officer keeps working.'
    case 'cancelled':
      return 'The explanation request was cancelled. The last explanation stays.'
    case 'superseded':
      return 'A new instruction superseded the explanation request. The last explanation stays.'
    case 'timed-out':
      return request.message ?? TIMEOUT_MESSAGE
    case 'failed':
      return request.message ?? 'The explanation request failed.'
    default:
      return request.message ?? ''
  }
}

export function failureOf(error: unknown): { status: RequestStatus; message: string } {
  const name =
    error !== null && typeof error === 'object' && typeof (error as { name?: unknown }).name === 'string'
      ? (error as { name: string }).name
      : ''
  const text = firstLine(String((error as { message?: unknown } | null)?.message ?? error ?? ''))
  if (name === 'AbortError' || /deadline|time[ -]?out|abort/i.test(text)) {
    return { status: 'timed-out', message: TIMEOUT_MESSAGE }
  }
  return {
    status: 'failed',
    message:
      text === ''
        ? 'The explanation request failed.'
        : `The explanation request failed: ${text.slice(0, 160)}`,
  }
}

// Compare-and-set the request only while the transition still owns it. A
// competing write that lands between the read and the set (a Keep preparing
// toggle, say) bumps the store version and makes the write miss without
// changing the generation; the transition reads again and retries so a
// settling request is never stranded. When the generation or status it
// depends on no longer matches, a newer request or instruction owns the
// request and the transition is discarded.
async function settleRequest(
  $: any,
  next: (state: BridgeState['request']) => BridgeState['request'] | null,
): Promise<boolean> {
  for (let attempt = 0; attempt < 16; attempt++) {
    const current = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
    const state = current?.value as BridgeState['request'] | null | undefined
    if (state === null || state === undefined) return false
    const value = next(state)
    if (value === null) return false
    const outcome = await $.state.set(
      { plugin: 'hermes-helmet', key: 'request' },
      value,
      { ifVersion: current.version },
    )
    if (outcome?.isSet === true) return true
  }
  return false
}

// Settles the request only if it is still the preparing generation: a late
// result of a cancelled or superseded request is discarded silently and the
// walkthrough is never touched.
async function failRequest(
  $: any,
  generation: number,
  status: RequestStatus,
  message: string | undefined,
): Promise<void> {
  await settleRequest($, state =>
    state.generation === generation && state.status === 'preparing'
      ? message === undefined
        ? { generation, status, retain: state.retain === true }
        : { generation, status, retain: state.retain === true, message }
      : null,
  )
}

async function installWalkthrough(
  $: any,
  generation: number,
  body: Walkthrough,
  snapshot: RequestSnapshot,
): Promise<void> {
  const current = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
  const state = current?.value as BridgeState['request'] | null | undefined
  if (state === null || state === undefined) return
  if (state.generation !== generation || state.status !== 'preparing') return
  // Adopt while this generation still owns the preparing request: the request
  // stays `preparing` until the walkthrough write has landed, so a duplicate
  // or newer Update cannot start a second preparation and race this write.
  // Only then does the request settle idle, and only while it is still the
  // preparing generation: a Cancel or supersession that won during the write
  // leaves it alone.
  //
  // The walkthrough is written under compare-and-set against the version read
  // here, so an older adoption can never overwrite a newer accepted
  // explanation: its write misses and is discarded. A Cancel or supersession
  // that wins while the write is in flight cannot stop it landing, so the
  // adoption rechecks the request afterwards and restores the last useful view
  // it replaced, only while its own write is still the current one.
  const key = { plugin: 'hermes-helmet', key: 'walkthrough' }
  const prior = await $.state.get(key)
  const adopted = await $.state.set(
    key,
    {
      body,
      readAt: snapshot.readAt,
      fingerprint: snapshot.fingerprint,
      preparedAt: new Date().toISOString(),
    },
    { ifVersion: prior?.version ?? 0 },
  )
  if (adopted?.isSet !== true) return
  const after = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
  const settled = after?.value as BridgeState['request'] | null | undefined
  if (settled?.generation !== generation || settled?.status !== 'preparing') {
    const landed = await $.state.get(key)
    await $.state.set(key, prior?.value ?? null, { ifVersion: landed?.version ?? 0 })
    return
  }
  await settleRequest($, s =>
    s.generation === generation && s.status === 'preparing'
      ? { generation, status: 'idle', retain: s.retain === true }
      : null,
  )
}

// A resolved `api-error`: the engine carries the HTTP status and the kind it
// classified the failure as, never the provider's own text.
export function providerFailureMessage(result: any): string {
  const kind = typeof result?.error === 'string' && result.error !== '' ? result.error : 'unknown'
  const status = typeof result?.status === 'number' ? result.status : null
  return status === null
    ? `The explanation request failed: the provider answered ${kind} and no response arrived.`
    : `The explanation request failed: the provider answered ${kind} (status ${status}).`
}

async function runPreparation(
  $: any,
  generation: number,
  controller: AbortController,
  snapshot: RequestSnapshot,
  options: ExplanationOptions,
): Promise<void> {
  try {
    // The AbortSignal is the call's own option (ModelCompleteOptions), not a
    // request field: it is what actually cuts the in-flight call on Cancel.
    // The engine never rejects over what the provider did; it resolves a
    // result, and the request's own time limit arrives as `aborted`.
    const result = await $.model.complete(
      {
        model: options.model,
        prompt: [
          { text: EXPLANATION_INSTRUCTIONS, cache: true },
          { text: JSON.stringify(WALKTHROUGH_SCHEMA) },
          { text: JSON.stringify(snapshot.summary) },
        ],
        maxTokens: MAX_TOKENS,
        timeoutMs: options.timeoutMs,
      },
      { signal: controller.signal },
    )
    if (result?.isAnswered !== true) {
      if (result?.reason === 'aborted') {
        // Its time limit elapsed, or the call was cut: timed out.
        await failRequest($, generation, 'timed-out', TIMEOUT_MESSAGE)
      } else if (result?.reason === 'api-error') {
        await failRequest($, generation, 'failed', providerFailureMessage(result))
      } else {
        // `empty-reply`, or a shape this release does not know.
        await failRequest($, generation, 'failed', EMPTY_MESSAGE)
      }
      return
    }
    const text = typeof result.text === 'string' ? result.text.trim() : ''
    if (text === '') {
      await failRequest($, generation, 'failed', EMPTY_MESSAGE)
      return
    }
    let parsed: unknown
    try {
      parsed = JSON.parse(text)
    } catch {
      await failRequest($, generation, 'failed', DIFFICULT_MESSAGE)
      return
    }
    if (validateWalkthrough(parsed, snapshot.summary) !== null) {
      await failRequest($, generation, 'failed', DIFFICULT_MESSAGE)
      return
    }
    await installWalkthrough($, generation, parsed as Walkthrough, snapshot)
  } catch (error) {
    const failure = failureOf(error)
    await failRequest($, generation, failure.status, failure.message)
  } finally {
    if (liveRequest !== null && liveRequest.generation === generation) liveRequest = null
  }
}

// The Update press: compare-and-set the request to a new preparing generation,
// take one record snapshot, then start exactly one model request off the press
// dispatch. A duplicate press while preparing is ignored.
export async function pressUpdate(
  $: any,
  helmetCommand: string,
  options: ExplanationOptions,
): Promise<void> {
  const current = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
  const state = current?.value as BridgeState['request'] | null | undefined
  if (state !== null && state !== undefined && state.status === 'preparing') return
  const generation = ((state?.generation ?? 0) as number) + 1
  const claimed = await $.state.set(
    { plugin: 'hermes-helmet', key: 'request' },
    {
      generation,
      status: 'preparing',
      retain: state?.retain === true,
      startedAt: new Date().toISOString(),
    },
    { ifVersion: current?.version ?? 0 },
  )
  if (claimed?.isSet !== true) return

  // The preparation claims its live request now, before the reader: Cancel and
  // a superseding instruction end a preparation by clearing this token and
  // aborting its controller, so every await below rechecks it locally and a
  // preparation that lost ownership adopts nothing and launches nothing.
  const controller = new AbortController()
  liveRequest = { generation, controller }
  const owns = () => liveRequest !== null && liveRequest.generation === generation
  const release = () => {
    if (owns()) liveRequest = null
  }

  const bound = await read($, binding)
  if (bound === null) {
    release()
    await failRequest($, generation, 'failed', 'No conversation is bound.')
    return
  }
  const outcome = await runReader($, helmetCommand, bound)
  // A Cancel or a new instruction during the reader ends this preparation.
  if (!owns()) return
  const stillBound = await read($, binding)
  if (stillBound === null || stillBound.sessionId !== bound.sessionId) {
    release()
    return
  }
  if (!outcome.ok) {
    release()
    await failRequest($, generation, 'failed', outcome.text)
    return
  }
  const summary = outcome.summary
  const snapshot: RequestSnapshot = {
    readAt: summary.readAt,
    fingerprint: summary.fingerprint,
    summary,
  }
  await update($, records, () => ({
    fingerprint: summary.fingerprint,
    readAt: summary.readAt,
    coverage: coverageOf(summary),
    summary,
  }))

  // One request, started off the press dispatch so the press returns at once.
  // A Cancel or a superseding instruction between scheduling and the deferred
  // start launches no stale request and never clobbers a newer controller.
  Promise.resolve(
    $.clock.after(0, () => {
      if (!owns()) return
      // The preparation runs in the background: its own failure is already
      // settled into the request state, and a settlement that arrives after
      // this plugin's environment is gone (a reload, a teardown) has nowhere
      // left to go, so it is discarded rather than left unhandled.
      void runPreparation($, generation, controller, snapshot, options).catch(() => {})
    }),
  ).catch(() => {})
}

// Cancel: abort, advance the generation and mark cancelled. The walkthrough is
// untouched, and the aborted request's later failure is discarded.
export async function pressCancel($: any): Promise<void> {
  const controller = liveRequest === null ? null : liveRequest.controller
  liveRequest = null
  const current = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
  const state = current?.value as BridgeState['request'] | null | undefined
  if (state !== null && state !== undefined && state.status === 'preparing') {
    // Bind the cancel to the generation it saw: a collision retries only while
    // that same preparing generation still owns the request, so a newer
    // request is never cancelled by accident.
    const generation = state.generation
    await settleRequest($, s =>
      s.status === 'preparing' && s.generation === generation
        ? { generation: generation + 1, status: 'cancelled', retain: s.retain === true }
        : null,
    )
  }
  if (controller !== null) controller.abort()
}

export async function toggleKeepPreparing($: any): Promise<void> {
  const current = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
  const state = current?.value as BridgeState['request'] | null | undefined
  if (state === null || state === undefined) return
  await $.state.set(
    { plugin: 'hermes-helmet', key: 'request' },
    { ...state, retain: state.retain !== true },
    { ifVersion: current.version },
  )
}

// A person's prompt while a request is preparing supersedes it, unless the
// Captain chose Keep preparing. The prompt itself is never rewritten, blocked
// or replaced: this only settles the request.
export async function notePrompt($: any, e: any): Promise<void> {
  const origin = e?.origin
  const kind = origin !== null && typeof origin === 'object' ? origin.kind : undefined
  if (kind === 'plugin') return
  const current = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
  const state = current?.value as BridgeState['request'] | null | undefined
  if (state === null || state === undefined) return
  if (state.status !== 'preparing' || state.retain === true) return
  const generation = state.generation
  const controller = liveRequest === null ? null : liveRequest.controller
  liveRequest = null
  await settleRequest($, s =>
    s.status === 'preparing' && s.retain !== true && s.generation === generation
      ? { generation: generation + 1, status: 'superseded', retain: false }
      : null,
  )
  if (controller !== null) controller.abort()
}

// After a reload, a preparing request with no live controller can never
// finish: it becomes failed, and the last useful walkthrough stays.
export async function recoverAfterReload(
  $: any,
  state: BridgeState['request'] | null | undefined,
  version: number | undefined,
): Promise<void> {
  if (state === null || state === undefined || state.status !== 'preparing') return
  if (liveRequest !== null) return
  await $.state.set(
    { plugin: 'hermes-helmet', key: 'request' },
    { ...state, status: 'failed', message: RELOAD_MESSAGE },
    version === undefined ? {} : { ifVersion: version },
  )
}

// ---------------------------------------------------------------------------
// Derived facts: time, actors, freshness. Everything comes from the saved
// records; nothing here is a judgment about delivery or stalls.

export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, '0')}s`
  return `${seconds}s`
}

function byRef(summary: RecordsSummary | null): Map<string, SessionRecord> {
  const map = new Map<string, SessionRecord>()
  if (summary !== null) for (const rec of summary.records) map.set(rec.ref, rec)
  return map
}

// Elapsed time from the first to the last cited record, or null when fewer
// than two cited records carry a usable timestamp.
export function elapsedOf(refs: string[], index: Map<string, SessionRecord>): string | null {
  const times: number[] = []
  for (const ref of refs) {
    const rec = index.get(ref)
    const at = rec === undefined ? NaN : Date.parse(rec.timestamp)
    if (!Number.isNaN(at)) times.push(at)
  }
  if (times.length < 2) return null
  return formatElapsed(Math.max(...times) - Math.min(...times))
}

export function actorOf(rec: SessionRecord): string {
  if (rec.role === 'tool_result') return rec.tool ? `Tool result (${rec.tool})` : 'Tool result'
  switch (rec.origin) {
    case 'person':
      return 'Captain'
    case 'assistant':
      return 'First officer'
    case 'agent':
      return 'Other agent'
    case 'hook':
      return 'Hook'
    case 'plugin':
      return 'Plugin'
    default:
      return 'Session metadata'
  }
}

function clock(timestamp: string): string {
  return timestamp.replace('T', ' ').replace(/\.\d+/, '').replace(/Z$/, ' UTC')
}

function excerpt(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > 240 ? `${flat.slice(0, 239)}…` : flat
}

function totalOf(fingerprint: string): number | null {
  const match = /^(\d+):/.exec(fingerprint)
  return match ? Number(match[1]) : null
}

// How many records the saved records hold beyond those the explanation read.
export function newerRecordCount(
  explained: { fingerprint: string; readAt: string },
  current: { fingerprint: string; summary: RecordsSummary },
): number {
  const was = totalOf(explained.fingerprint)
  const now = totalOf(current.fingerprint)
  if (was !== null && now !== null) return Math.max(0, now - was)
  const since = Date.parse(explained.readAt)
  return current.summary.records.filter(rec => Date.parse(rec.timestamp) > since).length
}

export function staleNote(
  saved: { fingerprint: string; readAt: string } | null,
  current: { fingerprint: string; summary: RecordsSummary } | null,
): string | null {
  if (saved === null || current === null || saved.fingerprint === current.fingerprint) {
    return null
  }
  const n = newerRecordCount(saved, current)
  return `Explanation read records at ${saved.readAt}; ${n} newer records since.`
}

export function coverageWarnings(summary: RecordsSummary | null): string[] {
  if (summary === null) return []
  const c = summary.coverage
  const out: string[] = []
  if (c.recordsIncluded < c.recordsTotal) {
    out.push(
      `Coverage is partial: ${c.recordsIncluded} of ${c.recordsTotal} records are included` +
        `${c.bytesOmitted > 0 ? `, ${c.bytesOmitted} bytes omitted` : ''}.`,
    )
  }
  if (c.unsupportedSkipped > 0) {
    out.push(`${c.unsupportedSkipped} unsupported records were skipped.`)
  }
  if (c.pendingTail) {
    out.push('The last line was still being written and was not read.')
  }
  for (const warning of summary.warnings) {
    out.push(typeof warning === 'string' ? warning : warning.message)
  }
  return out
}

// A link is shown only when its address appears verbatim in a record that
// the item cites; a link without that evidence is never drawn.
export function evidenceLinks(
  item: WalkthroughItem,
  index: Map<string, SessionRecord>,
): Array<{ label: string; url: string }> {
  const out: Array<{ label: string; url: string }> = []
  for (const link of item.links ?? []) {
    if (!/^https?:\/\//.test(link.url)) continue
    const backed = link.evidence.some(ref => index.get(ref)?.text.includes(link.url))
    if (backed) out.push({ label: link.label, url: link.url })
  }
  return out
}

// ---------------------------------------------------------------------------
// The view. One element tree from `$.ui.resolve(e)` serves the terminal and
// the desktop; the scroll window is the mod's own, in blocks, so Back can
// restore it exactly.

let blockCount = 0

// Press handlers are closures made in the render hook; `$` itself is never
// put in an object (the validator wants every call spelled out at its site).
type Actions = {
  open: (itemId: string) => Promise<void>
  back: () => Promise<void>
  toggle: (section: 'change' | 'sources') => Promise<void>
  refresh: () => Promise<void>
  run: (action: Action) => Promise<void>
  update: () => Promise<void>
  cancel: () => Promise<void>
  keep: () => Promise<void>
}

type Ctx = {
  ui: any
  act: Actions
  body: Walkthrough | null
  index: Map<string, SessionRecord>
  stack: Array<{ itemId?: string; scroll?: number; open?: string[] }>
  names: ActionNames
  busy: Action | null
  request: BridgeState['request']
}

// Long text is cut into word-wrapped pieces, each its own block, so the
// block-counted scroll window moves a line or so at a time and never skips
// a whole long explanation or source list.
const PIECE_WIDTH = 72

export function textPieces(text: string): string[] {
  const pieces: string[] = []
  for (const line of text.split('\n')) {
    let current = ''
    for (const word of line.split(/\s+/).filter(w => w !== '')) {
      if (current !== '' && current.length + 1 + word.length > PIECE_WIDTH) {
        pieces.push(current)
        current = ''
      }
      current = current === '' ? word : `${current} ${word}`
      while (current.length > PIECE_WIDTH) {
        pieces.push(current.slice(0, PIECE_WIDTH))
        current = current.slice(PIECE_WIDTH)
      }
    }
    pieces.push(current)
  }
  return pieces
}

function textBlocks(ui: any, key: string, text: string, dim = false): any[] {
  const { Text } = ui
  return textPieces(text).map((piece, i) => (
    <Text key={`${key}:${i}`} wrap="wrap" dimColor={dim ? true : undefined}>
      {piece === '' ? ' ' : piece}
    </Text>
  ))
}

function pairBoxes(ui: any, item: WalkthroughItem, suffix: string): any {
  const { Box, Text } = ui
  const reported = item.reported
  const disposition = item.disposition
  return (
    <Box key={`pair${suffix}`} flexDirection="column" marginY={1}>
      <Box
        key={`report${suffix}`}
        flexDirection="column"
        paddingX={1}
        backgroundColor={REPORT_BACKGROUND}
      >
        <Text bold>Completion report</Text>
        <Text wrap="wrap">
          {reported === undefined
            ? 'No completion report is recorded for this item.'
            : `${reported.actor}: ${reported.label}`}
        </Text>
      </Box>
      <Box
        key={`disposition${suffix}`}
        flexDirection="column"
        paddingX={1}
        backgroundColor={DISPOSITION_BACKGROUND}
      >
        <Text bold>Disposition</Text>
        <Text wrap="wrap">
          {disposition === undefined
            ? 'No acceptance or rejection is recorded. A completed run is not acceptance.'
            : `${disposition.actor}: ${disposition.label}`}
        </Text>
      </Box>
    </Box>
  )
}

function overviewBlocks(ctx: Ctx): any[] {
  const { Box, Text, Button } = ctx.ui
  const body = ctx.body as Walkthrough
  const blocks: any[] = []
  blocks.push(
    <Text key="objective-title" bold>
      Objective
    </Text>,
    ...textBlocks(ctx.ui, 'objective', body.objective),
    <Text key="outcome-title" bold>
      Outcome
    </Text>,
    ...textBlocks(ctx.ui, 'outcome', body.summary),
  )
  for (const [group, label] of GROUPS) {
    const items = body.items.filter(item => item.group === group)
    if (items.length === 0) continue
    blocks.push(
      <Text key={`group:${group}`} bold>
        {label}
      </Text>,
    )
    for (const item of items) {
      const paired = item.reported !== undefined || item.disposition !== undefined
      blocks.push(
        <Box key={`row:${item.id}`} flexDirection="column" marginBottom={1}>
          <Button
            key={`item:${item.id}`}
            plain
            onPress={() => ctx.act.open(item.id)}
          >
            {`${item.title} [${item.status}]`}
          </Button>
          <Text wrap="truncate-end" dimColor>
            {item.summary.replace(/\s+/g, ' ')}
          </Text>
          {paired ? pairBoxes(ctx.ui, item, `:${item.id}`) : null}
        </Box>,
      )
    }
  }
  return blocks
}

function detailBlocks(ctx: Ctx, item: WalkthroughItem, open: string[]): any[] {
  const { Box, Text, Button, Link } = ctx.ui
  const blocks: any[] = []
  const elapsed = elapsedOf(item.evidence, ctx.index)
  blocks.push(
    <Box key="title" flexDirection="column">
      <Text bold>{item.title}</Text>
      <Text dimColor>
        {item.status}
        {elapsed === null ? '' : ` · elapsed ${elapsed}`}
      </Text>
    </Box>,
  )
  blocks.push(
    <Text key="explanation-title" bold>
      Explanation
    </Text>,
    ...textBlocks(ctx.ui, 'explanation', item.detail),
  )
  if (item.steps !== undefined && item.steps.length > 0) {
    blocks.push(
      <Text key="steps-title" bold>
        Handoffs, reviews and repairs
      </Text>,
    )
    item.steps.forEach((step, i) => {
      const spent = elapsedOf(step.evidence, ctx.index)
      blocks.push(
        <Text key={`step:${i}`} bold wrap="wrap">
          {step.actor}: {step.label}
          {spent === null ? '' : ` (elapsed ${spent})`}
        </Text>,
        ...textBlocks(ctx.ui, `step:${i}:detail`, step.detail),
      )
    })
  }
  if (item.reported !== undefined || item.disposition !== undefined) {
    blocks.push(pairBoxes(ctx.ui, item, ''))
  }
  if (item.change !== undefined) {
    const shown = open.includes('change')
    const change = item.change
    blocks.push(
      <Button key="change-toggle" onPress={() => ctx.act.toggle('change')}>
        {shown ? 'Hide before and after' : 'View before and after'}
      </Button>,
    )
    if (shown) {
      blocks.push(
        <Text key="before-title" bold>
          Before
        </Text>,
        ...textBlocks(ctx.ui, 'before', change.before),
        <Text key="after-title" bold>
          After
        </Text>,
        ...textBlocks(ctx.ui, 'after', change.after),
        ...textBlocks(ctx.ui, 'change-explanation', change.explanation, true),
      )
    }
  }
  const links = evidenceLinks(item, ctx.index)
  if (links.length > 0) {
    blocks.push(
      <Box key="links" flexDirection="column" marginBottom={1}>
        <Text bold>Links</Text>
        {links.map(link => (
          <Link href={link.url} label={link.label} />
        ))}
      </Box>,
    )
  }
  const cited = item.evidence
    .map(ref => ctx.index.get(ref))
    .filter((rec): rec is SessionRecord => rec !== undefined)
  const sourcesOpen = open.includes('sources')
  blocks.push(
    <Button key="sources-toggle" onPress={() => ctx.act.toggle('sources')}>
      {sourcesOpen ? 'Hide supporting records' : `Show supporting records (${cited.length})`}
    </Button>,
  )
  if (sourcesOpen) {
    cited.forEach(rec => {
      blocks.push(
        <Text key={`source:${rec.ref}:head`} dimColor>
          {clock(rec.timestamp)} · {actorOf(rec)}
        </Text>,
        ...textBlocks(ctx.ui, `source:${rec.ref}`, excerpt(rec.text)),
      )
    })
  }
  return blocks
}

async function openItem($: any, itemId: string): Promise<void> {
  await update($, view, current => {
    const base = current.stack.length > 0 ? current.stack : [{ scroll: 0 }]
    return { ...current, stack: [...base, { itemId, scroll: 0, open: [] }] }
  })
}

async function goBack($: any): Promise<void> {
  await update($, view, current =>
    current.stack.length > 1 ? { ...current, stack: current.stack.slice(0, -1) } : current,
  )
}

async function toggleSection($: any, section: 'change' | 'sources'): Promise<void> {
  await update($, view, current => {
    const stack = current.stack.slice()
    const top = stack[stack.length - 1]
    if (top === undefined) return current
    const open = top.open ?? []
    stack[stack.length - 1] = {
      ...top,
      open: open.includes(section) ? open.filter(s => s !== section) : [...open, section],
    }
    return { ...current, stack }
  })
}

function preWalkthroughBlocks(ctx: Ctx, counts: Coverage | null): any[] {
  const { Box, Text } = ctx.ui
  const lines: string[] = []
  if (counts === null) {
    lines.push('No records have been read yet. Choose Refresh records to read this conversation.')
  } else {
    lines.push(`${counts.recordsIncluded} of ${counts.recordsTotal} records are read.`)
    if (counts.bytesOmitted > 0) lines.push(`${counts.bytesOmitted} bytes of tool output were left out.`)
    if (counts.unsupportedSkipped > 0) lines.push(`${counts.unsupportedSkipped} unsupported records were skipped.`)
    if (counts.pendingTail) lines.push('The last line was still being written.')
  }
  return [
    <Box key="no-walkthrough" flexDirection="column" marginBottom={1}>
      <Text wrap="wrap">No walkthrough has been prepared for this conversation yet.</Text>
      {lines.map(line => (
        <Text wrap="wrap">{line}</Text>
      ))}
    </Box>,
  ]
}

function footerBlocks(ctx: Ctx, summary: RecordsSummary | null, hasBody: boolean): any[] {
  const { Box, Text, Button } = ctx.ui
  const blocks: any[] = []
  if (hasBody && summary !== null) {
    const c = summary.coverage
    blocks.push(
      <Text key="counts" dimColor>
        {c.recordsIncluded} of {c.recordsTotal} records read
      </Text>,
    )
  }
  const preparing = ctx.request.status === 'preparing'
  blocks.push(
    <Box key="actions" flexDirection="column" marginTop={1}>
      <Button
        key="refresh"
        variant="primary"
        onPress={() => ctx.act.refresh()}
      >
        {summary === null ? 'Read records' : 'Refresh records'}
      </Button>
      <Box key="explanation" flexDirection="column">
        <Button key="update-explanation" onPress={() => ctx.act.update()}>
          {preparing ? "Preparing explanation…" : 'Update explanation'}
        </Button>
        {preparing ? (
          <Box key="preparing" flexDirection="column">
            <Button key="cancel" onPress={() => ctx.act.cancel()}>
              Cancel
            </Button>
            <Button key="keep-preparing" onPress={() => ctx.act.keep()}>
              {ctx.request.retain === true ? 'Stop keeping the request' : 'Keep preparing'}
            </Button>
          </Box>
        ) : null}
        <Text
          key="explanation-status"
          dimColor={!preparing && ctx.request.message === undefined}
          color={
            ctx.request.status === 'failed' || ctx.request.status === 'timed-out'
              ? 'error'
              : ctx.request.status === 'cancelled' || ctx.request.status === 'superseded'
                ? 'warning'
                : undefined
          }
          wrap="wrap"
        >
          {statusLine(ctx.request)}
        </Text>
      </Box>
      {(['show-me', 'retro'] as Action[]).map(action =>
        actionBlock(
          { Box, Text, Button },
          action,
          ctx.names[action],
          ctx.busy === action,
          () => ctx.act.run(action),
        ),
      )}
    </Box>,
  )
  return blocks
}

// ---------------------------------------------------------------------------

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
  const configured = (options as Record<string, unknown> | undefined)?.helmetCommand
  const names = actionNames(options)
  const explanation = explanationOptions(options)
  const helmetCommand =
    typeof configured === 'string' && configured.trim() !== '' ? configured.trim() : 'helmet'

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
    const current = await $.state.get({ plugin: 'hermes-helmet', key: 'request' })
    const request = (current?.value ?? null) as BridgeState['request'] | null
    const ended = request?.message === CHANGED_MESSAGE
    // A reload re-evaluates this module, so a preparing request whose live
    // controller is gone can never finish. It becomes failed here.
    await recoverAfterReload($, request, current?.version)
    await update($, binding, current => {
      if (current === null) return ended ? null : { sessionId: id }
      return current.sessionId === id ? current : null
    })
    return next(e)
  })

  // A person's prompt while an explanation is preparing supersedes it, unless
  // the Captain chose Keep preparing. The prompt always passes through
  // unchanged: this hook never blocks, rewrites or replaces it.
  on('prompt.submit', async ($, e, next) => {
    await notePrompt($, e)
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
    const opened = await $.ui.open({ id: PANE_ID, title: PANE_TITLE })
    if (!isSupported(version)) return { text: tooOldMessage(version) }
    // Opening reads the saved records (the reader only; no model, no turn).
    await discoverCommands($, names)
    await refreshRecords($, helmetCommand)
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

  // The mod owns its scroll window, counted in blocks, so Back can put the
  // window where it was. The engine's own window never moves.
  on('ui.scroll', { requestId: PANE_ID }, async ($, e) => {
    await update($, view, current => {
      const stack = current.stack.length > 0 ? current.stack.slice() : [{}]
      const top = stack[stack.length - 1]
      const step = e.by > 0 ? 1 : e.by < 0 ? -1 : 0
      const next = Math.min(Math.max(0, blockCount - 1), Math.max(0, (top.scroll ?? 0) + step))
      stack[stack.length - 1] = { ...top, scroll: next }
      return { ...current, stack }
    })
    return {}
  })

  on('ui.render', { component: 'Pane', requestId: PANE_ID }, async ($, e) => {
    const ui = $.ui.resolve(e)
    const { Box, Text, Button } = ui
    const { version } = await $.session.version()
    const bound = await read($, binding)
    const status = await read($, request)
    const saved = await read($, walkthrough)
    const current = await read($, records)
    const nav = await read($, view)

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
    const body: Walkthrough | null = saved === null ? null : saved.body
    const summary: RecordsSummary | null = current === null ? null : current.summary
    const stack = nav.stack.length > 0 ? nav.stack : [{}]
    const top = stack[stack.length - 1] as { itemId?: string; scroll?: number; open?: string[] }
    const act: Actions = {
      open: id => openItem($, id),
      back: () => goBack($),
      toggle: section => toggleSection($, section),
      refresh: async () => {
        await discoverCommands($, names)
        await refreshRecords($, helmetCommand)
      },
      run: action => runAction($, action, names[action]),
      update: () => pressUpdate($, helmetCommand, explanation),
      cancel: () => pressCancel($),
      keep: () => toggleKeepPreparing($),
    }
    if (availability === null) await discoverCommands($, names)
    const busy = await read($, pending)
    const ctx: Ctx = {
      ui,
      act,
      body,
      index: byRef(summary),
      stack,
      names,
      busy: busy === null ? null : busy.action,
      request: status,
    }

    // Header: identity, read freshness, the stale note, coverage warnings and
    // the last reader message. The substantive work follows it.
    const header: any[] = [
      <Text key="title" bold>
        {PANE_TITLE}
      </Text>,
      <Text key="conversation" dimColor wrap="wrap">
        Conversation {bound.sessionId}
      </Text>,
    ]
    if (current !== null) {
      header.push(
        <Text key="freshness" dimColor wrap="wrap">
          Records read at {current.readAt}
        </Text>,
      )
    }
    const stale = staleNote(saved, current)
    if (stale !== null) {
      header.push(
        <Text key="stale" color="warning" wrap="wrap">
          {stale}
        </Text>,
      )
    }
    coverageWarnings(summary).forEach((warning, i) => {
      header.push(
        <Text key={`warning:${i}`} color="warning" wrap="wrap">
          {warning}
        </Text>,
      )
    })
    if (nav.notice !== undefined) {
      header.push(
        <Box key="notice">
          <Text color={nav.notice.kind === 'error' ? 'error' : undefined} wrap="wrap">
            {nav.notice.text}
          </Text>
        </Box>,
      )
    }

    let blocks: any[]
    if (body === null) {
      blocks = preWalkthroughBlocks(ctx, current === null ? null : current.coverage)
    } else if (top.itemId === undefined) {
      blocks = overviewBlocks(ctx)
    } else {
      const item = body.items.find(candidate => candidate.id === top.itemId)
      // Back is part of the fixed header, so it stays reachable however far
      // a long detail has been scrolled.
      header.push(
        <Button key="back" onPress={() => ctx.act.back()}>
          Back
        </Button>,
      )
      if (item === undefined) {
        blocks = [
          <Text key="missing" wrap="wrap">
            This item is not part of the current explanation.
          </Text>,
        ]
      } else {
        blocks = detailBlocks(ctx, item, top.open ?? [])
      }
    }
    blocks = [...blocks, ...footerBlocks(ctx, summary, body !== null)]
    blockCount = blocks.length
    const first = Math.min(Math.max(0, top.scroll ?? 0), Math.max(0, blocks.length - 1))
    return (
      <Box flexDirection="column">
        {header}
        {blocks.slice(first)}
      </Box>
    )
  })
}
