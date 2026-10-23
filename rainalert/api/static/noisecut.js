/* The debug slider under the map: radar readings below a cut are not drawn (DESIGN.md D-79).

   For finding where radar noise should be cut - only on `/?debug`, and only in this browser: it
   changes what is drawn here, nothing that is stored, sent or warned about.

   Done without touching a pixel. A frame is a palette PNG, and its transparency is not per pixel
   but per colour, in one small chunk (`tRNS`, an alpha byte for each of the ~245 palette entries).
   So the frame's bytes are fetched - the tooltip fetches the same ones, mostly from the HTTP
   cache - every colour whose readings all lie below the cut gets alpha 0 in that chunk, the chunk's
   checksum is recomputed, and the map is handed the result as a `blob:` URL. The image data is
   never decoded, so a frame is cut in well under a millisecond and the loop keeps its pace.

   Which colour means which readings is the manifest's `values` table (overlay.py `value_table`),
   read through rainprobe.js. Frames from before D-78 have one colour for every reading below
   0.02 mm, so on those a cut between 0.001 and 0.01 hides nothing and one above 0.01 hides all
   of it.

   The slider starts at the noise floor (D-80), which is what everyone sees. Lower brings back
   what the floor removes: those readings are in every frame as transparent colours of their own,
   and get their alpha back from the table's `below`. Higher hides more. */
(function (global) {
  'use strict';

  /* The cuts the slider offers, in mm per 5 min: nothing hidden, then every fine drizzle reading
     (D-78), then hundredths to 0.1. Index 0 shows every echo, the floor among them included. */
  var CUTS = [0];
  for (var k = 1; k <= 20; k++) { CUTS.push(k / 1000); }
  for (var h = 3; h <= 10; h++) { CUTS.push(h / 100); }

  var KEEP = 40;   // cut frames kept as blob URLs - the slider's neighbourhood, a few MB

  var CRC_TABLE = (function () {
    var table = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var i = 0; i < 8; i++) { c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; }
      table[n] = c >>> 0;
    }
    return table;
  })();

  /* CRC-32 as PNG uses it (ISO 3309), over `bytes`. */
  function crc32(bytes) {
    var c = 0xffffffff;
    for (var i = 0; i < bytes.length; i++) { c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8); }
    return (c ^ 0xffffffff) >>> 0;
  }

  function chunk(type, data) {
    var out = new Uint8Array(12 + data.length);
    var view = new DataView(out.buffer);
    view.setUint32(0, data.length);
    for (var i = 0; i < 4; i++) { out[4 + i] = type.charCodeAt(i); }
    out.set(data, 8);
    view.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
    return out;
  }

  /* A frame's PNG bytes with every colour whose readings all lie below `cut` made transparent, and
     every reading under the noise floor that is not below `cut` drawn again. `lookup` maps an RGBA
     key to `{from, to, below, alpha}` (RainProbe.lookupTable). Returns the bytes unchanged when
     nothing changes - at the floor itself - and throws on anything that is not a palette PNG. */
  function cutPng(bytes, lookup, cut) {
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (view.getUint32(0) !== 0x89504e47) { throw new Error('not a PNG'); }
    var at = 8, plte = null, trns = null, trnsAt = -1, trnsEnd = -1, plteEnd = -1;
    while (at + 12 <= bytes.length) {
      var length = view.getUint32(at);
      var type = String.fromCharCode(bytes[at + 4], bytes[at + 5], bytes[at + 6], bytes[at + 7]);
      var end = at + 12 + length;
      if (end > bytes.length) { throw new Error('truncated PNG'); }
      if (type === 'PLTE') { plte = bytes.subarray(at + 8, at + 8 + length); plteEnd = end; }
      if (type === 'tRNS') { trns = bytes.subarray(at + 8, at + 8 + length); trnsAt = at; trnsEnd = end; }
      if (type === 'IDAT' || type === 'IEND') { break; }
      at = end;
    }
    if (!plte) { throw new Error('not a palette PNG'); }

    var count = plte.length / 3;
    var alpha = new Uint8Array(count);
    var changed = 0;
    for (var i = 0; i < count; i++) {
      var a = trns && i < trns.length ? trns[i] : 255;
      alpha[i] = a;
      var entry = lookup.get(global.RainProbe.rgbaKey(plte[i * 3], plte[i * 3 + 1],
        plte[i * 3 + 2], a));
      if (!entry) { continue; }
      var next = a;
      if (entry.below) {
        // Under the floor: drawn again unless it is under the cut as well.
        next = entry.to < cut - 1e-9 ? 0 : entry.alpha;
      } else if (entry.to !== null && entry.to < cut - 1e-9) {
        // Hidden only when every reading drawn in this colour is below the cut; the open-ended
        // top band never is.
        next = 0;
      }
      if (next !== a) {
        alpha[i] = next;
        changed++;
      }
    }
    if (!changed) { return bytes; }

    var replacement = chunk('tRNS', alpha);
    var before = trns ? bytes.subarray(0, trnsAt) : bytes.subarray(0, plteEnd);
    var after = trns ? bytes.subarray(trnsEnd) : bytes.subarray(plteEnd);
    var out = new Uint8Array(before.length + replacement.length + after.length);
    out.set(before, 0);
    out.set(replacement, before.length);
    out.set(after, before.length + replacement.length);
    return out;
  }

  /* What the slider says. `floor` is the noise floor (D-80), named when the cut is on it. */
  function label(cut, floor) {
    if (!cut) { return 'nichts ausgeblendet – jedes Radarecho wird gezeigt'; }
    var mm = cut.toLocaleString('de-DE', { minimumFractionDigits: cut < 0.02 ? 3 : 2 });
    return 'unter ' + mm + ' mm/5 min ausgeblendet (≈ '
      + (cut * 12).toLocaleString('de-DE', { maximumSignificantDigits: 2 }) + ' mm/h)'
      + (Math.abs(cut - floor) < 1e-9 ? ' – die Rauschgrenze, wie für alle' : '');
  }

  /* `box`: the slider's container, rendered only on `/?debug`, with the noise floor in
     `data-floor`. `timeline`: radar.js's, for the value table and to show frames through
     `filter`. */
  function attach(box, timeline) {
    if (!box || !timeline || !timeline.filter || !global.RainProbe || typeof fetch !== 'function'
        || typeof Blob !== 'function' || typeof URL === 'undefined' || !URL.createObjectURL) {
      return null;
    }
    var input = box.querySelector('input');
    var out = box.querySelector('output');
    if (!input) { return null; }
    // The floor everyone sees is where the slider starts (D-80); at it, frames are drawn as they
    // are and nothing is fetched.
    var floor = parseFloat((box.dataset && box.dataset.floor) || '0') || 0;
    var start = CUTS.findIndex(function (c) { return Math.abs(c - floor) < 1e-9; });
    input.min = 0;
    input.max = CUTS.length - 1;
    input.step = 1;
    input.value = String(start < 0 ? 0 : start);
    box.hidden = false;

    var cut = start < 0 ? 0 : floor;
    var cache = new Map();           // frame url -> Promise of the url to draw, oldest first
    var lookup = null, lookupFor = null;

    function table() {
      var raw = timeline.values();
      if (raw !== lookupFor) { lookup = raw ? global.RainProbe.lookupTable(raw) : null; lookupFor = raw; }
      return lookup;
    }

    // Revoked late: the map may still be holding the image a moment after it is replaced.
    function release(promise) {
      promise.then(function (url) {
        if (url.indexOf('blob:') === 0) { setTimeout(function () { URL.revokeObjectURL(url); }, 10000); }
      }, function () {});
    }

    function filtered(url) {
      if (cache.has(url)) {
        var hit = cache.get(url);
        cache.delete(url);
        cache.set(url, hit);
        return hit;
      }
      var mine = cut;
      var promise = fetch(url, { mode: 'cors', credentials: 'omit' })
        .then(function (response) {
          if (!response.ok) { throw new Error('frame ' + response.status); }
          return response.arrayBuffer();
        })
        .then(function (buffer) {
          var lookupNow = table();
          if (!lookupNow) { return url; }
          var bytes = new Uint8Array(buffer);
          var cutBytes = cutPng(bytes, lookupNow, mine);
          return cutBytes === bytes ? url
            : URL.createObjectURL(new Blob([cutBytes], { type: 'image/png' }));
        });
      // A frame that cannot be fetched or cut is drawn as it is (radar.js `show`), and tried
      // again next time rather than remembered as failed.
      promise.catch(function () { cache.delete(url); });
      cache.set(url, promise);
      while (cache.size > KEEP) {
        var oldest = cache.keys().next().value;
        release(cache.get(oldest));
        cache.delete(oldest);
      }
      return promise;
    }

    function apply() {
      var next = CUTS[+input.value] || 0;
      if (out) { out.textContent = label(next, floor); }
      if (next === cut) { return; }
      cut = next;
      cache.forEach(release);
      cache.clear();
      timeline.filter(Math.abs(cut - floor) < 1e-9 ? null : filtered);
    }

    input.addEventListener('input', apply);
    apply();
    return { cut: function () { return cut; } };
  }

  global.RainCut = { attach: attach, cutPng: cutPng, crc32: crc32, label: label, CUTS: CUTS };
})(typeof window !== 'undefined' ? window : globalThis);
