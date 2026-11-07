// A stand-in for @hermes/plugin-sdk: just enough for desktop/plugin.js to load, register
// and render outside Hermes Desktop. Hooks read canned data from globalThis.__dexioTest.

const state = () => globalThis.__dexioTest

export const PANES_AREA = 'panes'
export const PALETTE_AREA = 'palette'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }

const component = name => {
  const fn = props => ({ type: name, props })
  Object.defineProperty(fn, 'name', { value: name })
  return fn
}
export const Badge = component('Badge')
export const Codicon = component('Codicon')
export const EmptyState = props => ({ type: 'div', props: { children: [props.title, props.description] } })
export const ScrollArea = props => props.children
export const StatusDot = component('StatusDot')
export const Tip = props => props.children

export function atom(value) {
  return { get: () => value, set: v => { value = v } }
}

export const haptic = () => {}
export const profileColor = name => (name && name !== 'default' ? `hsl(${name.length * 40} 68% 58%)` : null)
export const relativeTime = () => 'just now'

export const host = {
  state: { focusedStoredSessionId: { get: () => state().focused } },
  notify: msg => state().notified.push(msg),
  revealPane: id => state().revealed.push(id),
  undismissPane: () => {}
}

export const queryClient = {
  getQueryData: key => state().queries[key[1]],
  invalidateQueries: ({ queryKey }) => state().invalidated.push(queryKey[1])
}

export function useQuery({ queryKey, queryFn }) {
  state().queryFns[queryKey[1]] = queryFn
  const data = state().queries[queryKey[1]]
  return { data, isLoading: data === undefined, isError: false }
}

export const useValue = a => a.get()
