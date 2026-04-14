// Smoke test for the browser-targeted wasm module, run under node.
// Needs node ≥ 23 for MEMORY64=1 binaries; fall back to wasm32 build
// (TARGET=wasm32 ./scripts/build.sh) on older node.
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const webDir = path.resolve(here, '..', 'web');

// meos.js is a UMD module expecting `window` or `document`; fake the minimum.
globalThis.document = { currentScript: { src: 'file://' + webDir + '/meos.js' } };
globalThis.window   = { location: { pathname: webDir + '/' } };

const { default: createMeos } = await import(path.join(webDir, 'meos.mjs')).catch(async () => {
  // meos.js is not ESM — load it as a script and pick up the global.
  const src = await readFile(path.join(webDir, 'meos.js'), 'utf8');
  // eslint-disable-next-line no-new-func
  new Function(src + '; globalThis.createMeos = createMeos;')();
  return { default: globalThis.createMeos };
});

const Module = await createMeos({
  locateFile: (p) => path.join(webDir, p),
});

const parse = Module.cwrap('meos_parse_tgeompoint', 'string', ['string']);
const json = parse('POINT(4.36 50.84)@2026-04-14 09:00:00');
if (!json || !json.includes('MovingPoint')) {
  console.error('FAIL:', json);
  process.exit(1);
}
const obj = JSON.parse(json);
if (obj.datetimes?.[0]?.slice(0, 10) !== '2026-04-14') {
  console.error('FAIL: unexpected timestamp', obj.datetimes);
  process.exit(1);
}
console.log('ok — parse returned', json.length, 'bytes');
