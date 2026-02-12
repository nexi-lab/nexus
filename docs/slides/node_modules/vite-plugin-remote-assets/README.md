# vite-plugin-remote-assets

[![NPM version](https://img.shields.io/npm/v/vite-plugin-remote-assets?color=a1b858&label=)](https://www.npmjs.com/package/vite-plugin-remote-assets)

Bundles your assets from remote URLs with your app

```html
<img src="http://example.com/image.jpg" />
```

To

```html
<img src="/node_modules/.remote-assets/f83j2f.jpg" />
```

## Install

```bash
npm i -D vite-plugin-remote-assets
```

```ts
// vite.config.ts
import RemoteAssets from 'vite-plugin-remote-assets'

export default {
  plugins: [
    RemoteAssets()
  ]
}
```

## Sponsors

<p align="center">
  <a href="https://cdn.jsdelivr.net/gh/antfu/static/sponsors.svg">
    <img src='https://cdn.jsdelivr.net/gh/antfu/static/sponsors.svg'/>
  </a>
</p>

## License

[MIT](./LICENSE) License © 2021 [Anthony Fu](https://github.com/antfu)
