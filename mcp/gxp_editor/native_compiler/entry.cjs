const util = require('node:util');

function writeGeneratorLog(...args) {
	process.stderr.write(`${util.format(...args)}\n`);
}

console.log = writeGeneratorLog;
console.info = writeGeneratorLog;
console.debug = writeGeneratorLog;

function createStorage() {
	const values = new Map();
	return {
		getItem(key) {
			return values.has(String(key)) ? values.get(String(key)) : null;
		},
		setItem(key, value) {
			values.set(String(key), String(value));
		},
		removeItem(key) {
			values.delete(String(key));
		},
		clear() {
			values.clear();
		},
	};
}

function createElement() {
	return {
		style: {},
		children: [],
		appendChild(child) {
			this.children.push(child);
			return child;
		},
		removeChild() {},
		setAttribute() {},
		removeAttribute() {},
		addEventListener() {},
		removeEventListener() {},
		getContext() {
			return {};
		},
		getBoundingClientRect() {
			return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
		},
	};
}

globalThis.window = globalThis;
globalThis.self = globalThis;
globalThis.top = globalThis;
globalThis.parent = globalThis;
globalThis.addEventListener = globalThis.addEventListener || (() => {});
globalThis.removeEventListener = globalThis.removeEventListener || (() => {});
globalThis.navigator = globalThis.navigator || { userAgent: 'gxp-action-design-compiler' };
globalThis.location = globalThis.location || {
	origin: 'http://127.0.0.1',
	href: 'http://127.0.0.1/',
	host: '127.0.0.1',
	hostname: '127.0.0.1',
	protocol: 'http:',
	pathname: '/',
};
globalThis.localStorage = globalThis.localStorage || createStorage();
globalThis.sessionStorage = globalThis.sessionStorage || createStorage();
globalThis.document = globalThis.document || {
	body: createElement(),
	documentElement: createElement(),
	head: createElement(),
	createElement,
	createElementNS: createElement,
	createTextNode(text) {
		return { textContent: text };
	},
	querySelector() {
		return null;
	},
	querySelectorAll() {
		return [];
	},
	getElementById() {
		return null;
	},
	addEventListener() {},
	removeEventListener() {},
};
globalThis.getComputedStyle = globalThis.getComputedStyle || (() => ({ getPropertyValue: () => '' }));
globalThis.matchMedia = globalThis.matchMedia || (() => ({
	matches: false,
	media: '',
	addListener() {},
	removeListener() {},
	addEventListener() {},
	removeEventListener() {},
}));
globalThis.requestAnimationFrame = globalThis.requestAnimationFrame || (callback => setTimeout(callback, 0));
globalThis.cancelAnimationFrame = globalThis.cancelAnimationFrame || clearTimeout;
globalThis.ResizeObserver = globalThis.ResizeObserver || class ResizeObserver {
	observe() {}
	unobserve() {}
	disconnect() {}
};
globalThis.MutationObserver = globalThis.MutationObserver || class MutationObserver {
	observe() {}
	disconnect() {}
};
globalThis.InbizConfig = globalThis.InbizConfig || {
	API_VERSION: '1',
	APP_ID: '',
	SITE_ID: '',
	BASE_URL: 'http://127.0.0.1',
};

const { executeCompilerCommand } = require('./bridge_core');
globalThis.__GXP_COMPILER_GENERATING__ = true;

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
	input += chunk;
});
process.stdin.on('end', () => {
	try {
		const payload = JSON.parse(input || '{}');
		const result = executeCompilerCommand(payload);
		process.stdout.write(JSON.stringify({ ok: true, ...result }), () => process.exit(0));
	} catch (error) {
		process.stdout.write(
			JSON.stringify({
				ok: false,
				error: error instanceof Error ? error.message : String(error),
			}),
			() => process.exit(1),
		);
	}
});

