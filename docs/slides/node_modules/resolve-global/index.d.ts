/**
Resolve the path of a globally installed module.

@param moduleName - What you would use in [`import()`](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Operators/import).
@returns The resolved path. Throws if the module cannot be found.

@example
```
// $ npm install --global cat-names

import {resolveGlobal} from 'resolve-global';

console.log(resolveGlobal('cat-names'));
//=> '/usr/local/lib/node_modules/cat-names'
```
*/
export function resolveGlobal(moduleName: string): string;

/**
Resolve the path of a globally installed module.

@param moduleName - What you would use in [`import()`](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Operators/import).
@returns The resolved path. Returns `undefined` instead of throwing if the module cannot be found.

@example
```
// $ npm install --global cat-names

import {resolveGlobalSilent} from 'resolve-global';

console.log(resolveGlobalSilent('cat-names'));
//=> '/usr/local/lib/node_modules/cat-names'
```
*/
export function resolveGlobalSilent(moduleName: string): string | undefined;
