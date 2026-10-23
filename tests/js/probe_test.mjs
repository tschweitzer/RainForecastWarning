/* The rain tooltip's script (rainprobe.js, D-77) against frames the renderer actually wrote.
 *
 * argv[2]: the served rainprobe.js. argv[3]: a JSON spec from tests/test_probe.py - PNG files to
 * decode, places to look up, the value table, and a wet and a dry place for the interaction
 * tests. argv[4]: where to write what was decoded, for the Python side to compare with what it
 * rendered. The decoding is checked there, against numpy; the interaction is checked here.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(process.argv[2], 'utf8');
const spec = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

function load(overrides = {}) {
  const names = ['window', 'document', 'setTimeout', 'clearTimeout', 'fetch', 'DecompressionStream'];
  const win = {};
  const values = [win, overrides.document, overrides.setTimeout || setTimeout,
    overrides.clearTimeout || clearTimeout, overrides.fetch || fetch,
    'DecompressionStream' in overrides ? overrides.DecompressionStream : DecompressionStream];
  new Function(...names, source)(...values);
  return win.RainProbe;
}

const Probe = load();
const output = { decoded: [], pixels: [], readings: [], described: [] };

// --- decoding --------------------------------------------------------------------------------
for (const item of spec.pngs) {
  const bytes = fs.readFileSync(item.path);
  const image = await Probe.decodeFrame(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length));
  fs.writeFileSync(item.out, Buffer.from(image.indices.buffer, image.indices.byteOffset, image.indices.length));
  output.decoded.push({ width: image.width, height: image.height, colours: Array.from(image.colours) });
}

await assert.rejects(Probe.decodeFrame(new Uint8Array([1, 2, 3, 4]).buffer), /not a PNG/);

for (const [lat, lon] of spec.places) {
  output.pixels.push(Probe.pixelAt(spec.bounds, spec.width, spec.height, lat, lon));
}
assert.equal(Probe.pixelAt(spec.bounds, spec.width, spec.height, 40, 10), -1, 'south of the image');
assert.equal(Probe.pixelAt(spec.bounds, spec.width, spec.height, 50, 20), -1, 'east of the image');

const frameBytes = fs.readFileSync(spec.frame_png);
const frameImage = await Probe.decodeFrame(frameBytes.buffer.slice(frameBytes.byteOffset, frameBytes.byteOffset + frameBytes.length));
const table = Probe.lookupTable(spec.values);
for (const [lat, lon] of spec.places) {
  output.readings.push(Probe.reading(frameImage, table, spec.bounds, lat, lon));
}
for (const entry of spec.describe) {
  output.described.push(Probe.describe(entry, { kind: 'observed', valid_time: '2026-10-18T15:40:00Z' }));
}
output.forecast = Probe.describe(null, { kind: 'forecast', valid_time: '2026-10-18T16:25:00Z' });

// --- the interaction ---------------------------------------------------------------------------

function element() {
  const el = {
    className: '', hidden: false, style: {}, children: [], attributes: {},
    offsetWidth: 120, offsetHeight: 40,
    setAttribute(name, value) { this.attributes[name] = value; },
    appendChild(child) { this.children.push(child); return child; },
    get text() {
      return [this._text || '', ...this.children.map((c) => c.text)].join(' ').trim();
    },
    set textContent(value) { this._text = value; this.children = []; },
    get textContent() { return this.text; }
  };
  return el;
}

function harness(options = {}) {
  let now = 0;
  let queue = [];
  let nextId = 1;
  const timers = {
    setTimeout(fn, ms) { const id = nextId++; queue.push({ id, at: now + ms, fn }); return id; },
    clearTimeout(id) { queue = queue.filter((t) => t.id !== id); }
  };
  function advance(ms) {
    now += ms;
    const due = queue.filter((t) => t.at <= now).sort((a, b) => a.at - b.at);
    queue = queue.filter((t) => t.at > now);
    due.forEach((t) => t.fn());
  }

  const listeners = {};
  const container = element();
  container.clientWidth = 400;
  container.getBoundingClientRect = () => ({ left: 0, top: 0 });
  container.addEventListener = (type, fn, opts) => {
    (listeners[type] = listeners[type] || []).push({ fn, capture: opts === true });
  };
  let fetches = 0;
  const pngBuffer = frameBytes.buffer.slice(frameBytes.byteOffset, frameBytes.byteOffset + frameBytes.length);
  const P = load({
    document: { createElement: () => element() },
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    fetch: async () => { fetches++; return { ok: true, arrayBuffer: async () => pngBuffer.slice(0) }; },
    ...(options.unsupported ? { DecompressionStream: undefined } : {})
  });

  // Container points the tests use: (100, 100) is on rain, (300, 100) is not.
  const places = { '100,100': spec.wet, '300,100': spec.dry, '102,101': spec.wet };
  const target = {
    container,
    latLngAt: (x, y) => {
      const at = places[x + ',' + y] || spec.dry;
      return { lat: at[0], lng: at[1] };
    }
  };
  const watchers = [];
  const state = { frame: { url: 'frame.png', kind: 'observed', valid_time: '2026-10-18T15:40:00Z' } };
  const src = {
    frame: () => state.frame, bounds: () => spec.bounds, values: () => spec.values,
    watch: (fn) => watchers.push(fn)
  };
  const api = P.attach(target, src);
  const tip = container.children[0];

  function fire(type, props = {}) {
    const event = {
      type, pointerType: 'touch', pointerId: 1, clientX: 0, clientY: 0, buttons: 0,
      stopped: false, prevented: false,
      target: { closest: () => (props.onControl ? {} : null) },
      stopPropagation() { this.stopped = true; },
      preventDefault() { this.prevented = true; },
      ...props
    };
    (listeners[type] || []).forEach((l) => l.fn(event));
    return event;
  }

  // Real time, not ticks: inflating a 1.5-megapixel frame takes a few stream chunks and a few
  // tens of milliseconds, and the probe's own timers are the fake ones above.
  async function settle() {
    await new Promise((r) => setTimeout(r, 400));
  }

  return { api, tip, fire, advance, settle, state, watchers, fetches: () => fetches };
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

test('a long press on rain says how much, and its tap does not place the pin', async () => {
  const h = harness();
  h.fire('pointerdown', { clientX: 100, clientY: 100 });
  h.advance(499);
  await h.settle();
  assert.equal(h.tip.hidden, true, 'not before half a second');
  h.advance(1);
  await h.settle();
  assert.equal(h.tip.hidden, false);
  assert.match(h.tip.text, /mm in 5 Minuten/);
  assert.match(h.tip.text, new RegExp(spec.wet_label));
  assert.match(h.tip.text, /gemessen/);
  assert.equal(h.fire('contextmenu').prevented, true, 'no long-press menu over the map');
  h.fire('pointerup');
  const click = h.fire('click');
  assert.equal(click.stopped, true, 'the map must not see the tap that ended the press');
  assert.equal(click.prevented, true);
  // Only that one: the next tap is an ordinary one again.
  h.fire('pointerdown', { clientX: 300, clientY: 100 });
  h.fire('pointerup');
  assert.equal(h.fire('click').stopped, false);
});

test('a tap is a tap', async () => {
  const h = harness();
  h.fire('pointerdown', { clientX: 100, clientY: 100 });
  h.advance(200);
  h.fire('pointerup');
  h.advance(1000);
  await h.settle();
  assert.equal(h.tip.hidden, true);
  assert.equal(h.fire('click').stopped, false, 'a normal tap still places the pin');
  assert.equal(h.fire('contextmenu').prevented, false);
});

test('a finger that moves is panning', async () => {
  const h = harness();
  h.fire('pointerdown', { clientX: 100, clientY: 100 });
  h.fire('pointermove', { clientX: 115, clientY: 100 });
  h.advance(600);
  await h.settle();
  assert.equal(h.tip.hidden, true);
});

test('two fingers are a pinch', async () => {
  const h = harness();
  h.fire('pointerdown', { clientX: 100, clientY: 100, pointerId: 1 });
  h.fire('pointerdown', { clientX: 150, clientY: 100, pointerId: 2 });
  h.advance(600);
  await h.settle();
  assert.equal(h.tip.hidden, true);
});

test('a long press where nothing is drawn says the picture shows no rain', async () => {
  const h = harness();
  h.fire('pointerdown', { clientX: 300, clientY: 100 });
  h.advance(500);
  await h.settle();
  assert.equal(h.tip.hidden, false);
  assert.match(h.tip.text, /zeigt das Radarbild keinen Regen/);
  assert.doesNotMatch(h.tip.text, /trocken/, 'dry and no data look the same; it must not claim dry');
});

test('a resting mouse on rain gets the tooltip, a moving one loses it', async () => {
  const h = harness();
  h.fire('pointermove', { pointerType: 'mouse', clientX: 100, clientY: 100 });
  h.advance(599);
  await h.settle();
  assert.equal(h.tip.hidden, true);
  h.advance(1);
  await h.settle();
  assert.equal(h.tip.hidden, false);
  h.fire('pointermove', { pointerType: 'mouse', clientX: 102, clientY: 101 });
  assert.equal(h.tip.hidden, false, 'a hand on a mouse is never quite still');
  h.fire('pointermove', { pointerType: 'mouse', clientX: 300, clientY: 100 });
  assert.equal(h.tip.hidden, true);
  h.advance(600);
  await h.settle();
  assert.equal(h.tip.hidden, true, 'resting on dry land shows nothing');
  h.fire('pointermove', { pointerType: 'mouse', clientX: 100, clientY: 100 });
  h.advance(600);
  await h.settle();
  assert.equal(h.tip.hidden, false);
  h.fire('pointerleave', { pointerType: 'mouse' });
  assert.equal(h.tip.hidden, true);
  assert.equal(h.fire('contextmenu', { pointerType: 'mouse' }).prevented, false,
    'a right click keeps its menu');
});

test('dragging with the mouse and the map controls read nothing', async () => {
  const h = harness();
  h.fire('pointermove', { pointerType: 'mouse', clientX: 100, clientY: 100, buttons: 1 });
  h.advance(600);
  await h.settle();
  assert.equal(h.tip.hidden, true);
  h.fire('pointermove', { pointerType: 'mouse', clientX: 100, clientY: 100, onControl: true });
  h.advance(600);
  await h.settle();
  assert.equal(h.tip.hidden, true);
  h.fire('pointerdown', { clientX: 100, clientY: 100, onControl: true });
  h.advance(600);
  await h.settle();
  assert.equal(h.tip.hidden, true);
  h.fire('pointerup');
  assert.equal(h.fire('click').stopped, false);
});

test('a change of frame re-reads the spot, and a gap takes the tooltip away', async () => {
  const h = harness();
  h.fire('pointermove', { pointerType: 'mouse', clientX: 100, clientY: 100 });
  h.advance(600);
  await h.settle();
  assert.equal(h.tip.hidden, false);
  h.state.frame = { url: 'frame.png', kind: 'forecast', valid_time: '2026-10-18T16:25:00Z' };
  h.watchers.forEach((fn) => fn(h.state.frame));
  await h.settle();
  assert.match(h.tip.text, /Vorhersage für/);
  assert.equal(h.fetches(), 1, 'the same frame is decoded once');
  h.state.frame = null;
  h.watchers.forEach((fn) => fn(null));
  await h.settle();
  assert.equal(h.tip.hidden, true);
});

test('without DecompressionStream there is no tooltip and nothing is intercepted', () => {
  const h = harness({ unsupported: true });
  assert.equal(h.api, null);
  assert.equal(h.tip, undefined);
});

let failed = 0;
for (const [name, fn] of tests) {
  try {
    await fn();
    console.log('ok -', name);
  } catch (error) {
    failed++;
    console.log('FAIL -', name);
    console.log(error);
  }
}
fs.writeFileSync(process.argv[4], JSON.stringify(output));
process.exit(failed ? 1 : 0);
