// Minimal observable store: get(), set(patch), subscribe(fn) -> unsubscribe.
export function createStore(initial) {
  let state = initial;
  const subs = new Set();
  return {
    get: () => state,
    set(patch) { state = { ...state, ...patch }; for (const f of [...subs]) f(state); },
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
  };
}
