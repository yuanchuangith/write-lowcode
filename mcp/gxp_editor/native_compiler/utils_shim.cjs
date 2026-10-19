const crypto = require('node:crypto');

function uuid(length = 32, radix = 16) {
	const size = Math.max(1, Number(length) || 32);
	const base = Math.max(2, Math.min(Number(radix) || 16, 62));
	const alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'.slice(0, base);
	const bytes = crypto.randomBytes(size);
	let result = '';
	for (let index = 0; index < size; index += 1) result += alphabet[bytes[index] % alphabet.length];
	return result;
}

const request = new Proxy(() => {
  if (globalThis.__GXP_COMPILER_GENERATING__) throw new Error('Network access is unavailable in the offline compiler');
  // Browser modules start async catalogue refreshes at import time. Suspend them;
  // do not make a request or fabricate a response for native generators.
  return new Promise(() => {});
}, {
  get(target, property) { return ['get', 'post', 'put', 'delete'].includes(property) ? target : Reflect.get(target, property); },
});
// Registration is inert: the offline compiler never dispatches a request.
request.interceptors = {request:{use() {}}, response:{use() {}}};
request.use = () => {};

module.exports = {
	uuid,
	getAppInfo: () => ({ IsPublish: false, AppId: '', SiteId: '' }),
	request,
	useApp: () => ({}),
	useInbiz: () => ({}),
};


