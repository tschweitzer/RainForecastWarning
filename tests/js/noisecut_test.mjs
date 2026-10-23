/* The debug noise cut (noisecut.js, D-79) on frames the renderer wrote.
 *
 * argv[2], argv[3]: the served rainprobe.js and noisecut.js. argv[4]: a JSON spec from
 * tests/test_noisecut.py - PNGs, the value table, and the cuts to apply. argv[5]: a directory to
 * write the cut PNGs to, which the Python side decodes with Pillow and compares pixel by pixel.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const win = {};
for (const file of [process.argv[2], process.argv[3]]) {
  new Function('window', fs.readFileSync(file, 'utf8'))(win);
}
const { RainProbe, RainCut } = win;
const spec = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const outDir = process.argv[5];
const lookup = RainProbe.lookupTable(spec.values);
const report = { unchanged: [], cuts: RainCut.CUTS, labels: RainCut.CUTS.map(RainCut.label) };

for (const item of spec.pngs) {
  const bytes = new Uint8Array(fs.readFileSync(item.path));
  for (const cut of spec.cuts) {
    const out = RainCut.cutPng(bytes, lookup, cut);
    if (out === bytes) { report.unchanged.push([item.name, cut]); }
    fs.writeFileSync(path.join(outDir, `${item.name}-${cut}.png`), out);
  }
}
assert.throws(() => RainCut.cutPng(new Uint8Array(16), lookup, 0.01), /not a PNG/);
// The check value every CRC-32 implementation is measured against.
assert.equal(RainCut.crc32(new TextEncoder().encode('123456789')), 0xcbf43926);

// --- the slider ------------------------------------------------------------------------------
const listeners = {};
const input = { value: '0', addEventListener: (type, fn) => { listeners[type] = fn; } };
const output = { textContent: '' };
const box = {
  hidden: true,
  querySelector: (selector) => (selector === 'input' ? input : output)
};
const filters = [];
const frameBytes = fs.readFileSync(spec.pngs[0].path);
const timeline = {
  values: () => spec.values,
  filter: (fn) => filters.push(fn)
};
const realFetch = globalThis.fetch;
let fetched = 0;
globalThis.fetch = async () => {
  fetched++;
  return { ok: true, arrayBuffer: async () => frameBytes.buffer.slice(frameBytes.byteOffset, frameBytes.byteOffset + frameBytes.length) };
};
win.RainCut.attach(box, timeline);
assert.equal(box.hidden, false, 'the slider shows itself once it works');
assert.equal(input.max, RainCut.CUTS.length - 1);
assert.match(output.textContent, /^aus/);
assert.deepEqual(filters, [], 'off is the default: frames drawn as they are');

input.value = String(RainCut.CUTS.indexOf(0.005));
listeners.input();
assert.equal(filters.length, 1);
assert.equal(typeof filters[0], 'function');
assert.match(output.textContent, /unter 0,005 mm\/5 min ausgeblendet/);
const drawn = await filters[0]('https://bucket.example/obs/1.png');
assert.match(drawn, /^blob:/, 'a cut frame is drawn from memory');
await filters[0]('https://bucket.example/obs/1.png');
assert.equal(fetched, 1, 'the same frame at the same cut is cut once');

input.value = '0';
listeners.input();
assert.equal(filters.at(-1), null, 'back to off: frames as they are');
globalThis.fetch = realFetch;

fs.writeFileSync(path.join(outDir, 'report.json'), JSON.stringify(report));
console.log('ok');
