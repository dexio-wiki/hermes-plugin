// react/jsx-runtime for the tests: elements are plain objects; render() below walks them.
export const jsx = (type, props, key) => ({ type, props: props || {}, key })
export const jsxs = jsx
export const Fragment = 'Fragment'
