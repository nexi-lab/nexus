import {createRequire} from 'node:module';
import globalDirectory from 'global-directory';

// TODO: Use `import.meta.resolve` when targeting Node.js 22.
const {resolve} = createRequire(import.meta.url);

export function resolveGlobal(moduleName) {
	return resolve(moduleName, {
		paths: [
			globalDirectory.yarn.packages,
			globalDirectory.npm.packages,
		],
	});
}

export function resolveGlobalSilent(moduleName) {
	try {
		return resolveGlobal(moduleName);
	} catch {}
}
