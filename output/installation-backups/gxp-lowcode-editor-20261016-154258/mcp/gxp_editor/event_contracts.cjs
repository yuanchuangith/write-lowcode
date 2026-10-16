'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const manifest = require('./event_contracts.json');
function enrich(contract, event, api) {
  const audited = manifest.contracts.filter(c => c.component_type === contract.component_type && c.event_name === contract.event_name);
  if (audited.length > 1) throw new Error('Ambiguous audited event contract');
  if (audited.length) {
    if (!api?.frontendRoot) throw new Error('Runtime source evidence requires the configured frontend repository');
    const root = path.resolve(api.frontendRoot);
    for (const item of audited[0].evidence) {
      const file = path.resolve(root, item.path);
      if (!file.startsWith(root + path.sep)) throw new Error('Runtime evidence path escapes frontend repository');
      const observed = crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
      if (observed !== item.sha256) throw new Error('Runtime event contract source changed; reverify ' + item.path);
    }
    return {...contract, ...audited[0], signature_status:'source_verified', event_type:event.eventType || null,
      input_params:audited[0].input_params.map(p=>({...p}))};
  }
  const complete = Array.isArray(event.params) && (contract.input_params.length > 0 || event.params.length === 0);
  return {...contract, event_type:event.eventType || null, signature_status:complete?'catalog_declared':'incomplete',
    runtime_verified:false, verification:complete?'catalog_only':'missing_input_contract'};
}
function requireComplete(contract) {
  if (contract.signature_status === 'incomplete') throw new Error('Incomplete event parameter contract for ' + contract.component_type + '.' + contract.event_name + '; verify the runtime callsite before binding');
  return contract;
}
function parametersMatch(actual, expected, contract, section) {
  if (actual.length !== expected.length) return false;
  const names = new Set();
  return expected.every((p,i)=>{
    const item = actual[i];
    if (typeof item?.title !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(item.title) || names.has(item.title)) return false;
    names.add(item.title);
    const compatible = item.type === p.type || (section === 'inputParams' && (contract.input_type_aliases?.[p.type] || []).includes(item.type));
    return compatible && (section === 'inputParams' || item.title === p.title);
  });
}
module.exports = {enrich, requireComplete, parametersMatch};
