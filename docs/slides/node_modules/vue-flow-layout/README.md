<h1 align="center">vue-flow-layout</h1>

<p align="center">🌊 The Vue waterfull layout component.</p>

<p align="center">
<a>
<img src="https://img.shields.io/npm/v/vue-flow-layout?style=flat&colorA=080f12&colorB=1fa669" alt="npm version" />
</a>
<a>
<img src="https://img.shields.io/npm/dm/vue-flow-layout?style=flat&colorA=080f12&colorB=1fa669" alt="npm downloads" />
</a>
<img src="https://img.shields.io/github/license/zyyv/vue-flow-layout.svg?style=flat&colorA=080f12&colorB=1fa669" alt="License" />
</a>
</p>

## Features

- 🌊 Waterfull layout
- 📦 Lightweight
- 🎨 Elegant transition
- 📱 Responsive

## Install

```bash
pnpm add vue-flow-layout
```

## Usage

```ts
import { createApp } from 'vue'
import VueFlowLayout from 'vue-flow-layout'
import App from './App.vue'

createApp(App).use(VueFlowLayout).mount('#app')
```

Use in your component:

```vue
<template>
  <FlowLayout>
    <div v-for="i in 100" :key="i" class="item">
      {{ i }}
    </div>
  </FlowLayout>
</template>
```

## Props

| Name       | Type                     | Default | Description                          |
| ---------- | ------------------------ | ------- | ------------------------------------ |
| cols       | number                   | 2       | The number of columns                |
| gap        | number \| [number, number] | 4       | The gap between items for X|Y       |
| duration   | number                   | 350ms   | The transition duration              |

## Credits

- [wc-flow-layout](https://github.com/huodoushigemi/wc-flow-layout)

## license

[MIT](./LICENSE)
