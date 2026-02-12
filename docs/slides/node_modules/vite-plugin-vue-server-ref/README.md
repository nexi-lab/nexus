# vite-plugin-vue-server-ref

[![NPM version](https://img.shields.io/npm/v/vite-plugin-vue-server-ref?color=a1b858&label=)](https://www.npmjs.com/package/vite-plugin-vue-server-ref)

Share state between clients and Vite server.

## Install

```bash
npm i -D vite-plugin-vue-server-ref
```

Add plugin to your `vite.config.ts`:

```ts
// vite.config.ts
import ServerRef from 'vite-plugin-vue-server-ref'

export default {
  plugins: [
    ServerRef({
      state: {
        /* Your custom initial state */
        foo: 'bar',
        object: {
          count: 0
        }
      }
    })
  ]
}
```

Use import it in your modules (`server-ref:[key]`)

```ts
import foo from 'server-ref:foo'

console.log(foo.value) // bar

foo.value = 'foobar'

// same as other modules / clients imported the server ref with same key
// or even refresh the pages
console.log(foo.value) // foobar
```

Or working with reactive object (`server-reactive:[key]`)

```ts
import object from 'server-reactive:object'

console.log(object.count) // 0
```

## Type Support

As server import can't infer the type correctly (by default it's `ServerRef<any>`), you can using `as` to specify the type.

```ts
import type { ServerReactive, ServerRef } from 'vite-plugin-vue-server-ref/client'
import _foo from 'server-ref:foo'
import _object from 'server-ref:object'

const foo = _foo as ServerRef<string>
const object = _object as ServerReactive<{ count: number }>

foo.value // string
object.count // number
```

## Controls

```ts
import foo from 'server-ref:foo'

foo.$syncUp = false // make it download only

foo.value = 'foobar' // won't send to server or other clients
```

```ts
import foo from 'server-ref:foo'

foo.$syncDown = false // make it upload only

// changes from other clients won't be received
```

```ts
import foo from 'server-ref:foo'

// listen to server change
foo.$onSet((value) => {
  console.log(`Changes from server: ${value}`)
})
```

## Diffing

When working with reactive objects, you can add `?diff` to make the syncing incremental (deep diff).

```ts
import object from 'server-ref:object?diff'

console.log(object) // { foo: ..., bar: ... }

object.foo.nested = 'bar'
// the patch will be sent as '{ foo: { nested: 'bar' }}}'
// instead of the entire object
```

## Sponsors

<p align="center">
  <a href="https://cdn.jsdelivr.net/gh/antfu/static/sponsors.svg">
    <img src='https://cdn.jsdelivr.net/gh/antfu/static/sponsors.svg'/>
  </a>
</p>

## License

[MIT](./LICENSE) License © 2021 [Anthony Fu](https://github.com/antfu)
