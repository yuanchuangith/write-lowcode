'use strict';
// Native CallInterface code generation reads interface metadata synchronously.
// Resolve only that lookup from fingerprint-bound canvas settings, never HTTP.
const crypto = require('node:crypto');

function snapshot(design) {
  const entries = new Map();
  for (const group of design.actionData || []) {
    if (group.actionType !== 'js') continue;
    for (const node of group.data || []) {
      if (node.elementKey !== 'CallInterface') continue;
      const api = node.paramsValue?.inputParams?.apiInfo;
      const setting = api && Object.hasOwn(api, 'setting') ? api.setting : api;
      const id = setting?.id, url = setting?.requestUrl;
      if (typeof id !== 'string' || !id.trim() || /[/?#\\]/.test(id)
          || typeof url !== 'string' || !url.trim()) {
        throw new Error('Offline CallInterface requires saved interface id and requestUrl; refresh the node configuration before planning');
      }
      if (entries.has(id) && entries.get(id) !== url) {
        throw new Error('Conflicting saved CallInterface requestUrl values for the same interface');
      }
      entries.set(id, url);
    }
  }
  return entries;
}

function describe(design) {
  const entries = [...snapshot(design)].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0);
  return {source: 'saved_canvas_interface_settings', interface_count: entries.length,
    snapshot_sha256: crypto.createHash('sha256').update(JSON.stringify(entries)).digest('hex'),
    live_metadata_checked: false, network_access: false};
}

function withSnapshot(design, compile) {
  const entries = snapshot(design);
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'XMLHttpRequest');
  const unavailable = () => { throw new Error('Offline compiler permits only saved CallInterface metadata lookups; network access is unavailable'); };
  class MetadataRequest {
    open(method, url, async) {
      if (this.opened || method !== 'GET' || async !== false || typeof url !== 'string') unavailable();
      const match = /(?:^|\/)api\/interfaces\/([^/?#\\]+)$/.exec(url);
      if (!match || !entries.has(match[1])) unavailable();
      this.id = match[1];
      this.opened = true;
    }
    setRequestHeader() { unavailable(); }
    send(body) {
      if (!this.opened || this.sent || body != null) unavailable();
      this.sent = true;
      this.status = 200;
      this.responseText = JSON.stringify({data: [{requestUrl: entries.get(this.id)}]});
    }
  }
  Object.defineProperty(globalThis, 'XMLHttpRequest', {value: MetadataRequest, configurable: true, writable: true});
  try { return compile(); }
  finally {
    if (previous) Object.defineProperty(globalThis, 'XMLHttpRequest', previous);
    else delete globalThis.XMLHttpRequest;
  }
}

module.exports = {withSnapshot, describe};
