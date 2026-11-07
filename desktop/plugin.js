// Dexio for Hermes Desktop: a pane that shows what your agent recalled from your Dexio
// wiki this turn, what it wrote there, and what your other agents changed recently.
//
// Plain ESM, loaded uncompiled by Hermes Desktop: jsx() calls instead of JSX, and only
// the three specifiers the loader allows. Data comes from this plugin's own backend
// (dashboard/plugin_api.py, /api/plugins/dexio/*) and from live plugin.dexio.* events the
// memory provider pushes; polling covers the cases where events cannot reach the window.

import {
  Badge,
  Codicon,
  EmptyState,
  PALETTE_AREA,
  PANES_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Tip,
  atom,
  haptic,
  host,
  profileColor,
  queryClient,
  relativeTime,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'dexio'
const PANE_ID = `${ID}:pane`
const KEYS = {
  activity: [ID, 'activity'],
  changes: [ID, 'changes'],
  status: [ID, 'status']
}
const WRITE_OPS = new Set(['write', 'edit', 'append'])
const OP_LABEL = { write: 'wrote', edit: 'edited', append: 'added to', delete: 'deleted', move: 'moved' }

// Live events make polling a slow safety net; without them it is the feed.
const $lastEvent = atom(0)
// After this agent writes, the next fetch of each skips the backend's short cache once.
const fresh = new Set()
function withFresh(kind, path) {
  if (!fresh.delete(kind)) return path
  return `${path}${path.includes('?') ? '&' : '?'}fresh=true`
}
const LIVE_WINDOW_MS = 60_000

let rest = null       // ctx.rest, bound in register()
let openUrl = null    // ctx.os.openExternal, bound in register()

function pollEvery(fastMs, slowMs) {
  return () => (Date.now() - $lastEvent.get() < LIVE_WINDOW_MS ? slowMs : fastMs)
}

function fetchJson(path) {
  if (!rest) return Promise.reject(new Error('Dexio plugin is not registered'))
  return rest(path)
}

function useActivity() {
  return useQuery({
    queryKey: KEYS.activity,
    queryFn: () => fetchJson('/activity?limit=100'),
    refetchInterval: pollEvery(4_000, 20_000),
    retry: 1
  })
}

function useChanges() {
  return useQuery({
    queryKey: KEYS.changes,
    queryFn: () => fetchJson(withFresh('changes', '/changes?limit=8')),
    refetchInterval: pollEvery(10_000, 20_000),
    retry: 1
  })
}

function useStatus() {
  return useQuery({
    queryKey: KEYS.status,
    queryFn: () => fetchJson(withFresh('status', '/status')),
    refetchInterval: 60_000,
    retry: 1
  })
}

// -- links into the wiki ------------------------------------------------------------

function wikiBase(status, fallbackUrl) {
  const ws = status && status.wiki
  if (ws && ws.url) return String(ws.url).replace(/\/+$/, '')
  return String(fallbackUrl || 'https://app.dexio.wiki').replace(/\/+$/, '')
}

function pageUrl(base, path) {
  const clean = String(path || '').replace(/^\/+/, '')
  return clean ? `${base}/${clean.split('/').map(encodeURIComponent).join('/')}` : base
}

function open(url) {
  haptic('tap')
  const go = openUrl ? openUrl(url) : Promise.resolve(false)
  Promise.resolve(go).then(ok => {
    if (!ok) host.notify({ kind: 'info', message: url, title: 'Open this page in your browser' })
  })
}

// -- what "this turn" means -----------------------------------------------------------

// The pane follows the chat the user is looking at: the newest recall in the focused session,
// plus the pages the agent read in that same turn. A fresh chat shows nothing yet. Desktop's
// route can carry a shortened session id, so ids match on a shared prefix. With no focus
// signal at all (an older Desktop), it falls back to the newest recall from a primary agent.
function sameSession(a, b) {
  if (!a || !b) return false
  return a === b || (Math.min(a.length, b.length) >= 12 && (a.startsWith(b) || b.startsWith(a)))
}

function currentTurn(entries, sessionId) {
  const primary = entries.filter(e => e.primary !== false)
  const pool = sessionId === undefined ? primary : primary.filter(e => sameSession(e.session_id, sessionId))
  const recall = pool.find(e => e.kind === 'recall')
  const anchor = recall || pool[0]
  if (!anchor) return { pages: [], query: '', at: null }
  const sameTurn = e => e.session_id === anchor.session_id && e.turn === anchor.turn
  const seen = new Map()
  for (const page of (recall && recall.pages) || []) {
    seen.set(page.path, { ...page, how: 'recalled' })
  }
  for (const e of pool) {
    if (e.kind !== 'tool' || !sameTurn(e) || !e.ok) continue
    if (e.op === 'read' && e.path && !seen.has(e.path)) {
      seen.set(e.path, { path: e.path, title: e.title || e.path, how: 'read' })
    }
  }
  return { pages: [...seen.values()], query: (recall && recall.query) || '', at: anchor.at }
}

export { currentTurn, knownTitles, pageUrl, wikiBase }

// Page titles the agent has seen (recalls, searches, reads), to label rows that only carry a path.
function knownTitles(entries, fromWiki) {
  const titles = new Map(Object.entries(fromWiki || {}))
  for (const e of [...entries].reverse()) {
    for (const p of e.pages || []) if (p.path && p.title) titles.set(p.path, p.title)
    if (e.op === 'read' && e.path && e.title) titles.set(e.path, e.title)
  }
  return titles
}

function writtenByThisAgent(entries) {
  return entries.filter(e => e.kind === 'tool' && e.ok && WRITE_OPS.has(e.op) && e.path).slice(0, 8)
}

function ms(at) {
  const t = Date.parse(at || '')
  return Number.isFinite(t) ? t : Date.now()
}

// -- pieces -----------------------------------------------------------------------------

const sectionTitle = 'mb-1.5 text-[0.625rem] font-medium uppercase tracking-[0.08em] text-(--ui-text-tertiary)'
const rowClass =
  'flex w-full min-w-0 items-start gap-2 rounded-[3px] px-1.5 py-1 text-left hover:bg-(--ui-control-hover-background)'

function Section({ title, detail, children }) {
  return jsxs('section', {
    className: 'flex flex-col px-3 pt-3',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between gap-2',
        children: [
          jsx('div', { className: sectionTitle, children: title }),
          detail ? jsx('div', { className: 'truncate text-[0.625rem] text-(--ui-text-quaternary)', children: detail }) : null
        ]
      }),
      children
    ]
  })
}

function Quiet({ children }) {
  return jsx('div', { className: 'px-1.5 py-1 text-xs text-(--ui-text-tertiary)', children })
}

function AgentName({ name }) {
  const color = profileColor(name)
  return jsx('span', {
    className: 'shrink-0 font-medium',
    style: color ? { color } : undefined,
    children: name || 'someone'
  })
}

function PageRow({ base, path, title, meta, children }) {
  return jsx(Tip, {
    label: path,
    children: jsxs('button', {
      type: 'button',
      className: rowClass,
      onClick: () => open(pageUrl(base, path)),
      children: [
        jsx(Codicon, { name: 'file', className: 'mt-0.5 shrink-0 text-(--ui-text-tertiary)', size: '12px' }),
        jsxs('span', {
          className: 'flex min-w-0 flex-1 flex-col',
          children: [
            jsx('span', { className: 'truncate text-xs text-(--ui-text-secondary)', children: title || path }),
            children ||
              (meta ? jsx('span', { className: 'truncate text-[0.6875rem] text-(--ui-text-tertiary)', children: meta }) : null)
          ]
        })
      ]
    })
  })
}

function UsedThisTurn({ entries, base, sessionId }) {
  const turn = currentTurn(entries, sessionId)
  return jsx(Section, {
    title: 'Used this turn',
    detail: turn.at ? relativeTime(ms(turn.at)) : null,
    children: turn.pages.length
      ? turn.pages.map(p =>
          jsx(
            PageRow,
            {
              base,
              path: p.path,
              title: p.title,
              meta:
                p.how === 'read'
                  ? 'read by the agent'
                  : `${p.lines || 0} matching line${p.lines === 1 ? '' : 's'}`
            },
            `${p.how}:${p.path}`
          )
        )
      : jsx(Quiet, {
          children: turn.at
            ? 'Nothing in the wiki matched this message.'
            : 'Send a message: the pages Dexio recalls for it show up here.'
        })
  })
}

function WrittenByAgent({ entries, base, agent, titles }) {
  const rows = writtenByThisAgent(entries)
  return jsx(Section, {
    title: 'Written by this agent',
    detail: agent ? `as ${agent}` : null,
    children: rows.length
      ? rows.map(e =>
          jsx(
            PageRow,
            {
              base,
              path: e.path,
              title: titles.get(e.path) || e.path,
              meta: `${OP_LABEL[e.op] || e.op} ${relativeTime(ms(e.at))}${e.note ? ` · ${e.note}` : ''}`
            },
            e.id
          )
        )
      : jsx(Quiet, { children: 'Nothing yet. When the agent files a page, it shows up here.' })
  })
}

function RecentChanges({ base, titles }) {
  const { data, isLoading } = useChanges()
  const rows = (data && data.changes) || []
  let body
  if (isLoading) body = jsx(Quiet, { children: 'Loading…' })
  else if (data && data.error) body = jsx(Quiet, { children: data.error })
  else if (!rows.length) body = jsx(Quiet, { children: 'No changes yet.' })
  else {
    body = rows.map(c =>
      jsxs(
        'button',
        {
          type: 'button',
          className: rowClass,
          onClick: () => open(pageUrl(base, c.deleted ? '' : c.path)),
          children: [
            jsx(Codicon, { name: 'history', className: 'mt-0.5 shrink-0 text-(--ui-text-tertiary)', size: '12px' }),
            jsxs('span', {
              className: 'flex min-w-0 flex-1 flex-col',
              children: [
                jsx('span', {
                  className: 'truncate text-xs text-(--ui-text-secondary)',
                  children: c.title || (titles && titles.get(c.path)) || c.path
                }),
                jsxs('span', {
                  className: 'flex min-w-0 gap-1 text-[0.6875rem] text-(--ui-text-tertiary)',
                  children: [
                    jsx(AgentName, { name: c.agent || c.person }),
                    jsx('span', { className: 'shrink-0', children: `${OP_LABEL[c.op] || c.op || 'changed'} it` }),
                    jsx('span', { className: 'truncate', children: `· ${relativeTime(ms(c.at))}` })
                  ]
                })
              ]
            })
          ]
        },
        `${c.revision}:${c.path}`
      )
    )
  }
  return jsx(Section, { title: 'Recent changes across your agents', children: body })
}

function Header({ status, base }) {
  const connected = Boolean(status && status.connected)
  const ws = (status && status.wiki) || {}
  const name = ws.name || 'Dexio'
  return jsxs('div', {
    className: 'flex items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsx(StatusDot, { tone: status ? (connected ? 'good' : 'bad') : 'muted' }),
      jsx('span', { className: 'min-w-0 flex-1 truncate text-xs font-medium', children: name }),
      connected && Number.isFinite(status.pages)
        ? jsx(Badge, { variant: 'muted', size: 'xs', children: `${status.pages} pages` })
        : null,
      jsx(Tip, {
        label: 'Open the wiki graph',
        children: jsx('button', {
          type: 'button',
          className:
            'grid size-5 shrink-0 place-items-center rounded-sm text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-foreground',
          onClick: () => open(base),
          children: jsx(Codicon, { name: 'type-hierarchy-sub', size: '13px' })
        })
      })
    ]
  })
}

// The durable id of the chat in focus; undefined on a Desktop without that atom.
const $focused = host.state && host.state.focusedStoredSessionId
function useFocusedSession() {
  return $focused ? useValue($focused) : undefined
}

function DexioPane() {
  const sessionId = useFocusedSession()
  const activity = useActivity()
  const status = useStatus()
  const entries = (activity.data && activity.data.entries) || []
  const base = wikiBase(status.data, activity.data && activity.data.url)
  const agent = (entries.find(e => e.agent) || {}).agent
  const changes = useChanges()
  const titles = knownTitles(entries, changes.data && changes.data.titles)

  if (activity.isError && !activity.data) {
    return jsx(EmptyState, {
      title: 'Dexio is not connected to this profile',
      description:
        'Add dexio to plugins.enabled in config.yaml and set DEXIO_API_KEY, then restart the gateway.'
    })
  }
  if (activity.data && activity.data.configured === false) {
    return jsx(EmptyState, {
      title: 'Set DEXIO_API_KEY',
      description: 'Make an agent key in Dexio under Settings > Agents and add it to this profile.'
    })
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col text-sm',
    children: [
      jsx(Header, { status: status.data, base }),
      jsxs('div', {
        className: 'flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto overflow-x-hidden pb-3',
        children: [
          jsx(UsedThisTurn, { entries, base, sessionId }),
          jsx(WrittenByAgent, { entries, base, agent, titles }),
          jsx(RecentChanges, { base, titles })
        ]
      })
    ]
  })
}

function StatusChip() {
  const status = useStatus()
  const data = status.data
  const label = data && data.connected && Number.isFinite(data.pages) ? `dexio · ${data.pages} pages` : 'dexio'
  return jsx(Tip, {
    label: data && data.connected ? 'Show the Dexio pane' : (data && data.error) || 'Dexio',
    children: jsxs('button', {
      type: 'button',
      className:
        'flex items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)',
      onClick: () => showPane(),
      children: [jsx(StatusDot, { tone: data ? (data.connected ? 'good' : 'bad') : 'muted' }), label]
    })
  })
}

function showPane() {
  haptic('tap')
  if (typeof host.undismissPane === 'function') host.undismissPane(PANE_ID)
  if (typeof host.revealPane === 'function') host.revealPane(PANE_ID)
}

function openGraph() {
  const status = queryClient.getQueryData(KEYS.status)
  const activity = queryClient.getQueryData(KEYS.activity)
  open(wikiBase(status, activity && activity.url))
}

function onActivityEvent(event) {
  $lastEvent.set(Date.now())
  queryClient.invalidateQueries({ queryKey: KEYS.activity })
  const payload = (event && event.payload) || {}
  if (payload.kind === 'tool' && WRITE_OPS.has(payload.op)) {
    fresh.add('changes')
    fresh.add('status')
    queryClient.invalidateQueries({ queryKey: KEYS.changes })
    queryClient.invalidateQueries({ queryKey: KEYS.status })
  }
}

export default {
  id: ID,
  name: 'Dexio',
  register(ctx) {
    rest = ctx.rest
    openUrl = url => (ctx.os && ctx.os.openExternal ? ctx.os.openExternal(url) : Promise.resolve(false))

    ctx.onEvent('plugin.dexio.recall', onActivityEvent)
    ctx.onEvent('plugin.dexio.tool', onActivityEvent)
    ctx.onDispose(() => {
      rest = null
      openUrl = null
    })

    ctx.registerMany([
      {
        id: 'pane',
        area: PANES_AREA,
        title: 'Dexio',
        data: { placement: 'right', width: '320px' },
        render: () => jsx(DexioPane, {})
      },
      {
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 125,
        render: () => jsx(StatusChip, {})
      },
      {
        id: 'open-pane',
        area: PALETTE_AREA,
        data: { id: 'dexio.open-pane', label: 'Open Dexio pane', keywords: ['dexio', 'wiki', 'memory'], run: showPane }
      },
      {
        id: 'open-graph',
        area: PALETTE_AREA,
        data: { id: 'dexio.open-graph', label: 'Open wiki graph', keywords: ['dexio', 'wiki', 'graph'], run: openGraph }
      }
    ])
  }
}
