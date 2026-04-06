import { createStore as createSolidStore, unwrap } from "solid-js/store";

type PartialState<T> = Partial<T> | ((state: T) => Partial<T>);
type Listener<T> = (state: T, previousState: T) => void;

export interface CompatStore<T extends object> {
  (): T;
  <U>(selector: (state: T) => U): U;
  getState(): T;
  setState(partial: PartialState<T>): void;
  subscribe(listener: Listener<T>): () => void;
}

export function createStore<T extends object>(
  initializer: (set: (partial: PartialState<T>) => void, get: () => T) => T,
): CompatStore<T> {
  const listeners = new Set<Listener<T>>();

  let state!: T;
  let setStateInternal!: ((...args: unknown[]) => void);

  const get = () => state;
  const set = (partial: PartialState<T>): void => {
    const previousState = { ...(unwrap(state) as T) };
    const nextPartial = typeof partial === "function" ? partial(state) : partial;
    setStateInternal(nextPartial);

    const nextState = state;
    for (const listener of listeners) {
      listener(nextState, previousState);
    }
  };

  const initialState = initializer(set, get);
  [state, setStateInternal] = createSolidStore(initialState);

  const store = ((selector?: (currentState: T) => unknown) => {
    if (!selector) return state;
    return selector(state);
  }) as CompatStore<T>;

  store.getState = get;
  store.setState = set;
  store.subscribe = (listener: Listener<T>) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  };

  return store;
}
