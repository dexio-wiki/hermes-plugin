// desktop/plugin.js outside Hermes Desktop: `node --test tests/desktop/plugin.test.mjs`.
// The SDK and React's jsx runtime are swapped for stubs, then the plugin registers into a
// fake context and its pane renders from canned backend data.
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, '../../desktop/plugin.js'), 'utf8')

// Only the three specifiers the Desktop loader allows may appear.
const specifiers = [...source.matchAll(/^\s*import\s[^'"]*['"]([^'"]+)['"]/gm)].map(m => m[1])

const work = mkdtempSync(join(tmpdir(), 'dexio-desktop-'))
const loadable = join(work, 'plugin.mjs')
writeFileSync(
  loadable,
  source
    .replace("'@hermes/plugin-sdk'", `'${pathToFileURL(join(here, 'sdk-stub.mjs')).href}'`)
    .replace("'react/jsx-runtime'", `'${pathToFileURL(join(here, 'jsx-stub.mjs')).href}'`)
)

function reset(queries = {}) {
  globalThis.__dexioTest = {
    queries, queryFns: {}, invalidated: [], notified: [], revealed: [], focused: 's1'
  }
}
reset()
const mod = await import(pathToFileURL(loadable).href)
const plugin = mod.default

function fakeContext() {
  const ctx = {
    contributions: [], listeners: {}, opened: [], restCalls: [],
    rest: path => { ctx.restCalls.push(path); return Promise.resolve({}) },
    os: { openExternal: url => { ctx.opened.push(url); return Promise.resolve(true) } },
    onEvent: (type, fn) => { ctx.listeners[type] = fn; return () => {} },
    onDispose: () => {},
    register: c => { ctx.contributions.push(c); return () => {} },
    registerMany: cs => { ctx.contributions.push(...cs); return () => {} }
  }
  return ctx
}

// Walk the element tree: call function components, collect text.
function render(node, out = []) {
  if (node == null || node === false) return out
  if (Array.isArray(node)) { node.forEach(n => render(n, out)); return out }
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out }
  if (typeof node.type === 'function') return render(node.type(node.props || {}), out)
  return render(node.props && node.props.children, out)
}

function find(node, pred, out = []) {
  if (node == null || typeof node !== 'object') return out
  if (Array.isArray(node)) { node.forEach(n => find(n, pred, out)); return out }
  if (typeof node.type === 'function') return find(node.type(node.props || {}), pred, out)
  if (pred(node)) out.push(node)
  return find(node.props && node.props.children, pred, out)
}

const ENTRIES = [
  { id: '5', kind: 'tool', op: 'write', path: 'customers/juniper', ok: true, session_id: 's1', turn: 2,
    agent: 'ops', at: '2026-11-07T18:00:05Z', note: 'Tuesday deliveries', primary: true },
  { id: '4', kind: 'tool', op: 'read', path: 'how-to/deliveries', title: 'Delivery routes', ok: true,
    session_id: 's1', turn: 2, agent: 'ops', at: '2026-11-07T18:00:03Z', primary: true },
  { id: '3', kind: 'recall', session_id: 's1', turn: 2, agent: 'ops', at: '2026-11-07T18:00:00Z',
    query: 'juniper deliveries', primary: true,
    pages: [{ path: 'customers/juniper', title: 'Juniper Street Cafe', lines: 2 }] },
  { id: '2', kind: 'recall', session_id: 's0', turn: 1, agent: 'ops', at: '2026-11-07T17:00:00Z',
    query: 'older', primary: true, pages: [{ path: 'old/page', title: 'Old page', lines: 1 }] },
  { id: '1', kind: 'tool', op: 'search', ok: true, session_id: 's0', turn: 1, agent: 'ops',
    at: '2026-11-07T16:59:00Z', primary: true, query: 'price',
    pages: [{ path: 'decisions/bag-price', title: 'Bag price', lines: 1 }] }
]

test('imports only what the Desktop loader allows', () => {
  assert.deepEqual([...new Set(specifiers)].sort(), ['@hermes/plugin-sdk', 'react/jsx-runtime'])
  assert.doesNotMatch(source, /(?<![\w$.])eval\(|new\s+Function\(|document\.|window\.hermesDesktop|import\(/)
})

test('registers the pane, the status chip, two palette commands and two live events', () => {
  const ctx = fakeContext()
  plugin.register(ctx)
  assert.equal(plugin.id, 'dexio')
  const byArea = area => ctx.contributions.filter(c => c.area === area)
  const [pane] = byArea('panes')
  assert.equal(pane.title, 'Dexio')
  assert.equal(pane.data.placement, 'right')
  assert.equal(byArea('statusBar.right').length, 1)
  assert.deepEqual(byArea('palette').map(c => c.data.label), ['Open Dexio pane', 'Open wiki graph'])
  assert.deepEqual(Object.keys(ctx.listeners).sort(), ['plugin.dexio.recall', 'plugin.dexio.tool'])
})

test('"this turn" follows the focused session and adds pages the agent read', () => {
  const turn = mod.currentTurn(ENTRIES, 's1')
  assert.deepEqual(turn.pages.map(p => [p.path, p.how]), [
    ['customers/juniper', 'recalled'], ['how-to/deliveries', 'read']])
  assert.deepEqual(mod.currentTurn(ENTRIES, 's0').pages.map(p => p.path), ['old/page'])
  assert.deepEqual(mod.currentTurn(ENTRIES, 'unknown').pages, [])                   // another chat
  assert.deepEqual(mod.currentTurn(ENTRIES, null).pages, [])                        // a fresh chat
  assert.equal(mod.currentTurn(ENTRIES, undefined).pages.length, 2)                 // older Desktop
  assert.deepEqual(mod.currentTurn([], 's1').pages, [])
  const long = ENTRIES.map(e => ({ ...e, session_id: `local-${e.session_id}-0123456789abcdef` }))
  assert.equal(mod.currentTurn(long, 'local-s1-0123456').pages.length, 2)          // shortened route id
  const sub = [{ ...ENTRIES[2], primary: false, session_id: 'sub' }]
  assert.deepEqual(mod.currentTurn(sub, 'sub').pages, [])
})

test('the pane renders the three sections from the backend', () => {
  reset({
    activity: { entries: ENTRIES, url: 'https://app.dexio.wiki', configured: true },
    status: { connected: true, pages: 14, wiki: { name: 'Wrenfield', url: 'https://app.dexio.wiki/w/demo1234' } },
    changes: { changes: [{ at: '2026-11-07T18:00:05Z', agent: 'support', path: 'decisions/bag-price', op: 'edit', revision: 9 }] }
  })
  const ctx = fakeContext()
  plugin.register(ctx)
  const pane = ctx.contributions.find(c => c.area === 'panes').render()
  const text = render(pane).join(' ')
  for (const part of ['Wrenfield', '14 pages', 'Used this turn', 'Juniper Street Cafe', '2 matching lines',
    'Delivery routes', 'read by the agent', 'Written by this agent', 'as ops', 'wrote just now · Tuesday deliveries',
    'Bag price',
    'Recent changes across your agents', 'support', 'edited it']) {
    assert.ok(text.includes(part), `pane shows "${part}" in: ${text}`)
  }
  const buttons = find(pane, n => n.type === 'button' && typeof n.props.onClick === 'function')
  const juniper = buttons.find(b => render(b).join(' ').includes('Juniper Street Cafe'))
  juniper.props.onClick()
  assert.deepEqual(ctx.opened, ['https://app.dexio.wiki/w/demo1234/customers/juniper'])

  const chip = ctx.contributions.find(c => c.area === 'statusBar.right').render()
  assert.ok(render(chip).join(' ').includes('dexio · 14 pages'))
})

test('live events refresh the pane, and a write refreshes changes past the cache', async () => {
  reset({ status: { connected: true, pages: 3, wiki: { url: 'https://app.dexio.wiki/w/abc' } } })
  const ctx = fakeContext()
  plugin.register(ctx)
  ctx.listeners['plugin.dexio.recall']({ type: 'plugin.dexio.recall', payload: { kind: 'recall' } })
  assert.deepEqual(globalThis.__dexioTest.invalidated, ['activity'])
  ctx.listeners['plugin.dexio.tool']({ type: 'plugin.dexio.tool', payload: { kind: 'tool', op: 'edit' } })
  assert.deepEqual(globalThis.__dexioTest.invalidated, ['activity', 'activity', 'changes', 'status'])

  const pane = ctx.contributions.find(c => c.area === 'panes').render()
  render(pane)                                      // registers the queries' fetchers
  await globalThis.__dexioTest.queryFns.changes()
  await globalThis.__dexioTest.queryFns.changes()
  assert.deepEqual(ctx.restCalls.filter(p => p.startsWith('/changes')),
    ['/changes?limit=8&fresh=true', '/changes?limit=8'])

  ctx.contributions.find(c => c.data && c.data.label === 'Open wiki graph').data.run()
  assert.deepEqual(ctx.opened, ['https://app.dexio.wiki/w/abc'])
  ctx.contributions.find(c => c.data && c.data.label === 'Open Dexio pane').data.run()
  assert.deepEqual(globalThis.__dexioTest.revealed, ['dexio:pane'])
})

test('rows that carry only a path use the titles the agent has seen', () => {
  const titles = mod.knownTitles(ENTRIES)
  assert.equal(titles.get('decisions/bag-price'), 'Bag price')
  assert.equal(titles.get('how-to/deliveries'), 'Delivery routes')
  assert.equal(titles.get('customers/juniper'), 'Juniper Street Cafe')
})

test('a profile without the backend or a key gets a plain explanation', () => {
  reset({ activity: { entries: [], configured: false } })
  const ctx = fakeContext()
  plugin.register(ctx)
  const text = render(ctx.contributions.find(c => c.area === 'panes').render()).join(' ')
  assert.ok(text.includes('Set DEXIO_API_KEY'), text)
})
