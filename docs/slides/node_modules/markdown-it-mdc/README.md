# markdown-it-mdc

[![npm version][npm-version-src]][npm-version-href]
[![npm downloads][npm-downloads-src]][npm-downloads-href]
[![bundle][bundle-src]][bundle-href]
[![JSDocs][jsdocs-src]][jsdocs-href]
[![License][license-src]][license-href]

[MDC (Markdown Components)](https://content.nuxtjs.org/guide/writing/mdc) for [`markdown-it`](https://github.com/markdown-it/markdown-it).

Use [VS Code MDC Extension](https://marketplace.visualstudio.com/items?itemName=Nuxt.mdc) for IDE support.

## Usage

```bash
npm i markdown-it markdown-it-mdc
```

```ts
import MarkdownIt from 'markdown-it'
import pluginMdc from 'markdown-it-mdc'

const md = new MarkdownIt()
  .use(pluginMdc)

const result = md.render(`
# Hello

Hello **World** with :my-component{.text-red name="foo"}!
`)
```

## Features

This plugin implements all the syntaxes documented in [MDC Syntax](https://content.nuxtjs.org/guide/writing/mdc). We are still testing behavior compatibility in detail before reaching `v0.1.0`.

- [x] [Block Component](https://content.nuxtjs.org/guide/writing/mdc#block-components)
  - [x] [Nesting](https://content.nuxtjs.org/guide/writing/mdc#nesting)
  - [x] [YAML Props](https://content.nuxtjs.org/guide/writing/mdc#yaml-props)
  - [x] [Slots](https://content.nuxtjs.org/guide/writing/mdc#slots)
- [x] [Inline Components](https://content.nuxtjs.org/guide/writing/mdc#inline-components)
- [x] [Inline Props](https://content.nuxtjs.org/guide/writing/mdc#props)
- [x] [Span](https://content.nuxtjs.org/guide/writing/mdc#span-text)
- ~~Frontmatter~~. Frontmatter is not built-in in this plugin, we recommend using [`@mdit-vue/plugin-frontmatter`](https://github.com/mdit-vue/mdit-vue/tree/main/packages/plugin-frontmatter) instead.

## Sponsors

<p align="center">
  <a href="https://cdn.jsdelivr.net/gh/antfu/static/sponsors.svg">
    <img src='https://cdn.jsdelivr.net/gh/antfu/static/sponsors.svg'/>
  </a>
</p>

## License

[MIT](./LICENSE) License © 2022 [Anthony Fu](https://github.com/antfu)

<!-- Badges -->

[npm-version-src]: https://img.shields.io/npm/v/markdown-it-mdc?style=flat&colorA=080f12&colorB=1fa669
[npm-version-href]: https://npmjs.com/package/markdown-it-mdc
[npm-downloads-src]: https://img.shields.io/npm/dm/markdown-it-mdc?style=flat&colorA=080f12&colorB=1fa669
[npm-downloads-href]: https://npmjs.com/package/markdown-it-mdc
[bundle-src]: https://img.shields.io/bundlephobia/minzip/markdown-it-mdc?style=flat&colorA=080f12&colorB=1fa669&label=minzip
[bundle-href]: https://bundlephobia.com/result?p=markdown-it-mdc
[license-src]: https://img.shields.io/github/license/antfu/markdown-it-mdc.svg?style=flat&colorA=080f12&colorB=1fa669
[license-href]: https://github.com/antfu/markdown-it-mdc/blob/main/LICENSE
[jsdocs-src]: https://img.shields.io/badge/jsdocs-reference-080f12?style=flat&colorA=080f12&colorB=1fa669
[jsdocs-href]: https://www.jsdocs.io/package/markdown-it-mdc
