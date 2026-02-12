# CoderMirror <samp>--theme-vars</samp>

A customizable CodeMirror theme using CSS variables

[![NPM version](https://img.shields.io/npm/v/codemirror-theme-vars?color=a1b858)](https://www.npmjs.com/package/codemirror-theme-vars)

- [prism-theme-vars](https://github.com/antfu/prism-theme-vars)

## Usage

```bash
npm i codemirror-theme-vars
```

```ts
import 'codemirror-theme-vars/base.css'
```

```ts
new CoderMirror({
  theme: 'vars'
})
```

or

```html
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/codemirror-theme-vars/base.css">
```

## Configuration

Add CSS variables in your CSS, For example:

```css
@import "codemirror-theme-vars/base.css";

:root {
  --cm-foreground: #393a34;
  --cm-background: #fbfbfb;
  --cm-comment: #b8c4b8;
  --cm-string: #c67b5d;
  --cm-literal: #3a9c9b;
  --cm-keyword: #248459;
  --cm-function: #849145;
  --cm-deleted: #a14f55;
  --cm-class: #2b91af;
  --cm-builtin: #a52727;
  --cm-property: #ad502b;
  --cm-namespace: #c96880;
  --cm-punctuation: #8e8f8b;
  --cm-decorator: #bd8f8f;
  --cm-json-property: #698c96;
}
```

See [base.css](./base.css) for more options avaliable.

### Built-in Themes

We have a few themes variables built-in that you can directly use.

```css
@import "codemirror-theme-vars/base.css";
@import "codemirror-theme-vars/themes/vitesse-light.css";

/* Overrides */
:root {
  --cm-background: #ffffff;
}
```

See all themes avaliable under [themes/*](./themes). Contributions are also greatly welcome! 

### Work with `prism-theme-vars`

```css
@import "codemirror-theme-vars/base.css";
@import "codemirror-theme-vars/to-prism.css";

/* --cm prefix will alias to --prism automatically */
:root {
  --cm-foreground: #393a34;
  --cm-background: #fbfbfb;
}
```

### Dark mode

Here is an example of Tailwind CSS favored dark mode support.

```css
html:not(.dark) {
  --cm-foreground: #393a34;
  --cm-background: #f8f8f8;

  --cm-comment: #758575;
  --cm-namespace: #444444;
  --cm-string: #bc8671;
  --cm-punctuation: #80817d;
  --cm-literal: #36acaa;
  --cm-keyword: #248459;
  --cm-function: #849145;
  --cm-deleted: #9a050f;
  --cm-class: #2b91af;
  --cm-builtin: #800000;
  --cm-property: #ce9178;
  --cm-regex: #ad502b;
}

html.dark {
  --cm-foreground: #d4d4d4;
  --cm-background: #1e1e1e;

  --cm-namespace: #aaaaaa;
  --cm-comment: #758575;
  --cm-namespace: #444444;
  --cm-string: #ce9178;
  --cm-punctuation: #d4d4d4;
  --cm-literal: #36acaa;
  --cm-keyword: #38a776;
  --cm-function: #dcdcaa;
  --cm-deleted: #9a050f;
  --cm-class: #4ec9b0;
  --cm-builtin: #d16969;
  --cm-property: #ce9178;
  --cm-regex: #ad502b;
}
```

## License

MIT
