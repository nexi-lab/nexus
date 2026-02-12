# diff-match-patch-es

[![npm version][npm-version-src]][npm-version-href]
[![npm downloads][npm-downloads-src]][npm-downloads-href]
[![bundle][bundle-src]][bundle-href]
[![JSDocs][jsdocs-src]][jsdocs-href]
[![License][license-src]][license-href]

ESM and TypeScript rewrite of Google's [diff-match-patch](https://github.com/google/diff-match-patch) (for JavaScript).

## Features

- Rewritten in ESM and TypeScript, ships with type declarations
- Published as dual ESM/CJS formats
- Refactor all to pure functions, fully tree-shakable

## Migration

Migration from [`diff-match-patch` npm package](https://github.com/JackuB/diff-match-patch)

- Default export and the class constructor has been removed
- Function name has been unified to camelCase
- Previous options like `Diff_Timeout` and `Diff_EditCost` are now passed as an options object in the arguments if needed

```js
// before
import DiffMatchPatch from 'diff-match-patch'

const dmp = new DiffMatchPatch()
dmp.Diff_Timeout = 1
const result = dmp.diff_main('hello', 'world')
```

```js
// after
import { diff } from 'diff-match-patch-es'

const result = diff('hello', 'world', { diffTimeout: 1 })
```

## Why?

Well, the code source repo from Google [hasn't been updated for 5 years](https://github.com/google/diff-match-patch), and the npm package `diff-match-patch` [hasn't been published for 4 years](https://github.com/JackuB/diff-match-patch). And the code is not ESM nor tree-shakable. While this library is a composition of `diff` `patch` and `match` functions, sometimes you don't need all of them but since all functions were bound to a single class, all the code has to be included in the bundle. It's probably not a big deal as this whole library is still relatively small, but it's a bit annoying to me when you know something wasn't optimal. So I took an afternoon porting and rewriting the library to ESM and TypeScript. Helpfully it will lead to better maintainability and help the community to move forward with the modern JavaScript ecosystem.

## Sponsors

<p align="center">
  <a href="https://cdn.jsdelivr.net/gh/antfu/static/sponsors.svg">
    <img src='https://cdn.jsdelivr.net/gh/antfu/static/sponsors.svg'/>
  </a>
</p>

## License

[Apache-2.0](./LICENSE) License

<!-- Badges -->

[npm-version-src]: https://img.shields.io/npm/v/diff-match-patch-es?style=flat&colorA=080f12&colorB=1fa669
[npm-version-href]: https://npmjs.com/package/diff-match-patch-es
[npm-downloads-src]: https://img.shields.io/npm/dm/diff-match-patch-es?style=flat&colorA=080f12&colorB=1fa669
[npm-downloads-href]: https://npmjs.com/package/diff-match-patch-es
[bundle-src]: https://img.shields.io/bundlephobia/minzip/diff-match-patch-es?style=flat&colorA=080f12&colorB=1fa669&label=minzip
[bundle-href]: https://bundlephobia.com/result?p=diff-match-patch-es
[license-src]: https://img.shields.io/github/license/antfu/diff-match-patch-es.svg?style=flat&colorA=080f12&colorB=1fa669
[license-href]: https://github.com/antfu/diff-match-patch-es/blob/main/LICENSE
[jsdocs-src]: https://img.shields.io/badge/jsdocs-reference-080f12?style=flat&colorA=080f12&colorB=1fa669
[jsdocs-href]: https://www.jsdocs.io/package/diff-match-patch-es
