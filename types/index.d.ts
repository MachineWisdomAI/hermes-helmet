// Type contract for the Hermes Helmet Claude Code mod (Captain's Bridge).
//
// Self-contained on purpose: Claude Code checks that this file has no import,
// export-from, require or reference, exports only types, and augments
// 'claude-code' only through `declare module`. The state shape below is the
// accepted design from issue #55; later slices add behavior, not fields.

export type Ref = string

export type Actor = 'Captain' | 'First officer' | 'Hermes' | 'Other agent'

export type WalkthroughItem = {
  id: string
  title: string
  group: 'changed' | 'unresolved' | 'activity'
  status: string
  summary: string
  detail: string
  evidence: Ref[]
  steps?: Array<{
    actor: Actor
    label: string
    detail: string
    evidence: Ref[]
  }>
  reported?: { label: string; actor: Actor; evidence: Ref[] }
  disposition?: { label: string; actor: Actor; evidence: Ref[] }
  change?: {
    before: string
    after: string
    explanation: string
    evidence: Ref[]
  }
  links?: Array<{ label: string; url: string; evidence: Ref[] }>
}

export type Walkthrough = {
  objective: string
  summary: string
  evidence: Ref[]
  items: WalkthroughItem[]
}

export type Coverage = {
  recordsTotal: number
  recordsIncluded: number
  bytesOmitted: number
  unsupportedSkipped: number
  pendingTail: boolean
}

export type RecordOrigin =
  | 'person'
  | 'hook'
  | 'plugin'
  | 'meta'
  | 'assistant'
  | 'agent'

export type SessionRecord = {
  ref: Ref
  parentRef: Ref | null
  agentId: string | null
  timestamp: string
  role: 'user' | 'assistant' | 'tool_result' | 'system'
  origin: RecordOrigin
  text: string
  tool?: string
  toolUseId?: string
  isError?: boolean
  truncated: boolean
}

export type RecordsSummary = {
  schema: 'hermes-helmet.bridge.records/1'
  helmetVersion: string
  sessionId: string
  readAt: string
  fingerprint: string
  records: SessionRecord[]
  coverage: Coverage
  // The reader reports `{ code, message }` objects; plain strings are
  // tolerated by the view.
  warnings: Array<string | { code: string; message: string }>
}

export type RequestStatus =
  | 'idle'
  | 'preparing'
  | 'failed'
  | 'cancelled'
  | 'superseded'
  | 'timed-out'

export type BridgeState = {
  binding: { sessionId: string; transcriptPath?: string } | null
  records: {
    fingerprint: string
    readAt: string
    coverage: Coverage
    summary: RecordsSummary
  } | null
  walkthrough: {
    body: Walkthrough
    readAt: string
    fingerprint: string
    preparedAt: string
  } | null
  request: {
    generation: number
    status: RequestStatus
    retain: boolean
    startedAt?: string
    message?: string
  }
  view: {
    // The last entry is the current view; an entry without itemId is the
    // overview. `scroll` is the window offset left behind when the next
    // view was opened; `open` lists the expanded sections of an item.
    stack: Array<{
      itemId?: string
      scroll?: number
      open?: Array<'change' | 'sources'>
    }>
    // The last reader or action message; it never replaces a useful view.
    notice?: { kind: 'error' | 'info'; text: string }
  }
  pending: { action: 'show-me' | 'retro'; since: string } | null
}

declare module 'claude-code' {
  interface PluginState {
    'hermes-helmet': {
      binding: BridgeState['binding']
      records: BridgeState['records']
      walkthrough: BridgeState['walkthrough']
      request: BridgeState['request']
      view: BridgeState['view']
      pending: BridgeState['pending']
    }
  }
}
