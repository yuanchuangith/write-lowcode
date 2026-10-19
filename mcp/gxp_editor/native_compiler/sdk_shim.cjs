'use strict';
// These SDK hooks render browser UI only. Calling them during generation is unsupported.
const unavailable = () => { throw new Error('Browser SDK execution is unavailable in the offline compiler'); };
module.exports = new Proxy({}, {get(_target, key) {
  if (key === '__esModule') return false;
  return unavailable;
}});
