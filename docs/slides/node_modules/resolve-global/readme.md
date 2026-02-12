# resolve-global

> Resolve the path of a globally installed module

## Install

```sh
npm install resolve-global
```

## Usage

```sh
npm install --global cat-names
```

```js
import {resolveGlobal} from 'resolve-global';

console.log(resolveGlobal('cat-names'));
//=> '/usr/local/lib/node_modules/cat-names'
```

## API

### resolveGlobal(moduleName)

Throws if the module cannot be found.

### resolveGlobalSilent(moduleName)

Returns `undefined` instead of throwing if the module cannot be found.

#### moduleName

Type: `string`

What you would use in [`import()`](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Operators/import).

## Related

- [import-global](https://github.com/sindresorhus/import-global) - Import a globally installed module
- [resolve-from](https://github.com/sindresorhus/resolve-from) - Resolve the path of a module from a given path
- [import-from](https://github.com/sindresorhus/import-from) - Import a module from a given path
- [is-installed-globally](https://github.com/sindresorhus/is-installed-globally) - Check if your package was installed globally
- [global-directory](https://github.com/sindresorhus/global-directory) - Get the directory of globally installed packages and binaries
