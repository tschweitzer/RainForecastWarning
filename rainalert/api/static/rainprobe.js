/* How much rain is falling where the reader points (DESIGN.md D-77).

   Resting the mouse on the map, or pressing a finger on it for half a second, shows what the radar
   frame on screen says at that spot: "ca. 0,4 mm in 5 Minuten", the hourly rate and the band name.

   Read from the frame itself, not asked of the server. An overlay is a palette PNG whose palette
   index *is* the reading, to within one of 32 shades per band (radar/overlay.py `palette_index`),
   and the timeline manifest says which readings each colour stands for (`value_table`). So the
   browser fetches the PNG it is already showing - from its HTTP cache, mostly - inflates it and
   looks the pixel up. Decoded here rather than drawn into a canvas and read back, because a canvas
   premultiplies alpha: at the palest band's alpha of 140 that rounds neighbouring shades into each
   other, and the drizzle shades are one value apart.

   Nothing is sent anywhere a frame is not already fetched from, and nothing is stored.

   Only colour is reported. A transparent pixel is dry *or* has no radar data - the image cannot
   tell them apart - so hovering there shows nothing, and a long press says that the picture shows
   no rain rather than that it is dry. */
(function (global) {
  'use strict';

  var HOVER_MS = 600;        // a resting mouse, not one passing over
  var PRESS_MS = 500;        // the long-press convention on both mobile platforms
  var MOUSE_SLOP_PX = 4;     // a hand on a mouse is never quite still
  var TOUCH_SLOP_PX = 10;    // a finger moves more than that and is panning, not pressing
  var CACHED_FRAMES = 4;     // 1.5 MB of indices each
  var MAX_PIXELS = 4e6;      // refuse to allocate for a file that claims to be enormous
  // Controls, pins and the tooltip itself are not map: hovering the zoom buttons reads nothing.
  var NOT_MAP = '.leaflet-control, .maplibregl-ctrl, .leaflet-marker-icon, .maplibregl-marker, '
    + '.rain-probe';

  /* ---- reading a frame ------------------------------------------------------------------- */

  /* The chunks of a PNG: IHDR's fields, PLTE, tRNS and the IDAT data joined. Throws on anything
     that is not an 8-bit, non-interlaced palette PNG, which is all the renderer writes. */
  function parsePng(bytes) {
    var signature = [137, 80, 78, 71, 13, 10, 26, 10];
    for (var i = 0; i < 8; i++) {
      if (bytes[i] !== signature[i]) { throw new Error('not a PNG'); }
    }
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    var at = 8, header = null, plte = null, trns = null, idat = [], idatLength = 0;
    while (at + 8 <= bytes.length) {
      var length = view.getUint32(at);
      var type = String.fromCharCode(bytes[at + 4], bytes[at + 5], bytes[at + 6], bytes[at + 7]);
      var start = at + 8, end = start + length;
      if (end + 4 > bytes.length) { throw new Error('truncated PNG'); }
      if (type === 'IHDR') {
        header = {
          width: view.getUint32(start), height: view.getUint32(start + 4),
          bitDepth: bytes[start + 8], colourType: bytes[start + 9],
          compression: bytes[start + 10], filter: bytes[start + 11], interlace: bytes[start + 12]
        };
      } else if (type === 'PLTE') {
        plte = bytes.subarray(start, end);
      } else if (type === 'tRNS') {
        trns = bytes.subarray(start, end);
      } else if (type === 'IDAT') {
        idat.push(bytes.subarray(start, end));
        idatLength += length;
      } else if (type === 'IEND') {
        break;
      }
      at = end + 4;   // past the CRC
    }
    if (!header || !plte || !idat.length) { throw new Error('incomplete PNG'); }
    if (header.bitDepth !== 8 || header.colourType !== 3 || header.compression !== 0
        || header.filter !== 0 || header.interlace !== 0) {
      throw new Error('not an 8-bit non-interlaced palette PNG');
    }
    if (!header.width || !header.height || header.width * header.height > MAX_PIXELS) {
      throw new Error('implausible PNG size');
    }
    var data = new Uint8Array(idatLength), offset = 0;
    idat.forEach(function (part) { data.set(part, offset); offset += part.length; });
    header.plte = plte;
    header.trns = trns;
    header.data = data;
    return header;
  }

  function inflate(bytes) {
    var stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate'));
    return new Response(stream).arrayBuffer().then(function (buffer) {
      return new Uint8Array(buffer);
    });
  }

  /* Undoes the PNG row filters (RFC 2083 §6) for one byte per pixel, giving the palette index of
     every pixel, row by row from the top. */
  function unfilter(raw, width, height) {
    var stride = width + 1;
    if (raw.length < stride * height) { throw new Error('short image data'); }
    var out = new Uint8Array(width * height);
    for (var y = 0; y < height; y++) {
      var kind = raw[y * stride], src = y * stride + 1, row = y * width, up = row - width;
      for (var x = 0; x < width; x++) {
        var a = x > 0 ? out[row + x - 1] : 0;
        var b = y > 0 ? out[up + x] : 0;
        var v = raw[src + x];
        if (kind === 1) {
          v += a;
        } else if (kind === 2) {
          v += b;
        } else if (kind === 3) {
          v += (a + b) >> 1;
        } else if (kind === 4) {
          var c = x > 0 && y > 0 ? out[up + x - 1] : 0;
          var p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c);
          v += pa <= pb && pa <= pc ? a : (pb <= pc ? b : c);
        } else if (kind !== 0) {
          throw new Error('unknown PNG filter ' + kind);
        }
        out[row + x] = v & 255;
      }
    }
    return out;
  }

  function rgbaKey(r, g, b, a) { return ((r << 24) | (g << 16) | (b << 8) | a) >>> 0; }

  /* A frame's PNG bytes -> `{width, height, indices, colours}`, where `colours[i]` is palette
     entry i's RGBA key. A palette entry without a tRNS byte is opaque, as the format says. */
  function decodeFrame(buffer) {
    var png = null;
    // A rejected promise rather than a throw, whatever is wrong with the file.
    return Promise.resolve().then(function () {
      png = parsePng(new Uint8Array(buffer));
      return inflate(png.data);
    }).then(function (raw) {
      var count = png.plte.length / 3, colours = new Uint32Array(256);
      for (var i = 0; i < count; i++) {
        var alpha = png.trns && i < png.trns.length ? png.trns[i] : 255;
        colours[i] = rgbaKey(png.plte[i * 3], png.plte[i * 3 + 1], png.plte[i * 3 + 2], alpha);
      }
      return {
        width: png.width, height: png.height,
        indices: unfilter(raw, png.width, png.height), colours: colours
      };
    });
  }

  function mercatorY(lat) { return Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360)); }

  /* The pixel a place falls in, as `row * width + col`, or -1 outside the image. The overlay is
     placed by its corners and stretched linearly in Web Mercator - by Leaflet's imageOverlay and by
     MapLibre's image source alike - which is also how the renderer computed it
     (radar/overlay.py `build_projection`). */
  function pixelAt(bounds, width, height, lat, lon) {
    var south = bounds[0][0], west = bounds[0][1], north = bounds[1][0], east = bounds[1][1];
    var col = Math.floor((lon - west) / (east - west) * width);
    var top = mercatorY(north);
    var row = Math.floor((top - mercatorY(lat)) / (top - mercatorY(south)) * height);
    if (!(col >= 0 && col < width && row >= 0 && row < height)) { return -1; }
    return row * width + col;
  }

  /* The manifest's `values` (overlay.py `value_table`) keyed by colour. */
  function lookupTable(values) {
    var table = new Map();
    ((values && values.rows) || []).forEach(function (row) {
      table.set(rgbaKey(row[0], row[1], row[2], row[3]), {
        from: row[4], to: row[5], label: values.bands[row[6]] || ''
      });
    });
    return table;
  }

  /* What a decoded frame says at a place: `{from, to, label}`, or null where nothing is drawn. */
  function reading(image, table, bounds, lat, lon) {
    var at = pixelAt(bounds, image.width, image.height, lat, lon);
    if (at < 0) { return null; }
    var index = image.indices[at];
    if (index === 0) { return null; }
    return table.get(image.colours[index]) || null;
  }

  /* ---- saying it ------------------------------------------------------------------------- */

  // Two significant figures: the reading is good to a few percent at best (one shade), and
  // "0,4372 mm" would claim far more than that. Significant figures rather than decimals so the
  // fine drizzle readings (D-78) keep theirs: 0,004 is not 0.
  function number(x) {
    return x.toLocaleString('de-DE', { maximumSignificantDigits: 2 });
  }

  function clock(iso) {
    return new Date(iso).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  }

  /* The tooltip's lines for a reading (null: the picture shows no rain here) in a frame. The
     hourly figure is the 5-minute one times twelve - "if it kept raining this hard" - which is how
     the legend labels the bands too, hence "≈". */
  function describe(entry, frame) {
    var when = frame.kind === 'forecast'
      ? 'Vorhersage für ' + clock(frame.valid_time) + ' Uhr'
      : 'gemessen ' + clock(frame.valid_time) + ' Uhr';
    if (!entry) {
      return { lines: ['Hier zeigt das Radarbild keinen Regen.'], when: when };
    }
    // DWD's "detected, 0.000 mm": an echo with no amount to it (D-78) - one of the readings the
    // noise question is about, so it is named rather than shown as "ca. 0 mm".
    if (entry.from === 0 && entry.to === 0) {
      return {
        lines: ['weniger als 0,001 mm in 5 Minuten', 'Radarecho ohne messbare Menge · '
          + entry.label],
        when: when
      };
    }
    if (entry.to === null) {
      return {
        lines: [number(entry.from) + ' mm oder mehr in 5 Minuten',
          number(entry.from * 12) + ' mm/h oder mehr · ' + entry.label],
        when: when
      };
    }
    var mid = (entry.from + entry.to) / 2;
    return {
      lines: ['ca. ' + number(mid) + ' mm in 5 Minuten', '≈ ' + number(mid * 12) + ' mm/h · '
        + entry.label],
      when: when
    };
  }

  /* ---- the interaction ------------------------------------------------------------------- */

  function supported() {
    return typeof DecompressionStream === 'function' && typeof fetch === 'function'
      && typeof Map === 'function';
  }

  /* `target`: `{container, latLngAt(x, y)}` from the map engine (radar.js / radar-gl.js
     `probeTarget`), x and y relative to the container. `source`: the timeline, for the frame on
     screen, its bounds and the value table, and to hear when the frame changes. */
  function attach(target, source) {
    if (!supported() || !target || !source || !source.frame) { return null; }
    var container = target.container;

    var tip = document.createElement('div');
    tip.className = 'rain-probe';
    tip.hidden = true;
    // Polite: said after whatever the reader is doing, and only when it changes.
    tip.setAttribute('role', 'status');
    container.appendChild(tip);

    var frames = new Map();          // url -> Promise of a decoded frame, oldest first
    var timer = null;
    var shown = null;                // {point, kind} while the tooltip is up
    var press = null;                // the touch point while a long press may be under way
    var fingers = new Set();
    var swallowClick = false;        // the tap a long press ends in must not place the pin
    var generation = 0;              // only the newest reading may change the tooltip
    var table = null, tableFor = null;

    function frameImage(url) {
      if (frames.has(url)) {
        var hit = frames.get(url);
        frames.delete(url);
        frames.set(url, hit);
        return hit;
      }
      var loading = fetch(url, { mode: 'cors', credentials: 'omit' })
        .then(function (response) {
          if (!response.ok) { throw new Error('frame ' + response.status); }
          return response.arrayBuffer();
        })
        .then(decodeFrame);
      // A failure is not kept: the next try may be after the network came back.
      loading.catch(function () { frames.delete(url); });
      frames.set(url, loading);
      while (frames.size > CACHED_FRAMES) { frames.delete(frames.keys().next().value); }
      return loading;
    }

    function values() {
      var raw = source.values();
      if (raw !== tableFor) { table = raw ? lookupTable(raw) : null; tableFor = raw; }
      return table;
    }

    function cancel() {
      if (timer) { clearTimeout(timer); timer = null; }
    }

    function hide() {
      generation++;
      shown = null;
      tip.hidden = true;
    }

    function point(event) {
      var box = container.getBoundingClientRect();
      return { x: event.clientX - box.left, y: event.clientY - box.top };
    }

    function far(a, b, limit) {
      return Math.abs(a.x - b.x) > limit || Math.abs(a.y - b.y) > limit;
    }

    function onMap(event) {
      return !(event.target && event.target.closest && event.target.closest(NOT_MAP));
    }

    /* Above the point, clear of the finger on touch and of the cursor with a mouse, and kept
       inside the map; below it when there is no room above. */
    function place(at, kind) {
      var width = tip.offsetWidth, height = tip.offsetHeight;
      var room = container.clientWidth, gap = kind === 'touch' ? 44 : 12;
      var left = kind === 'touch' ? at.x - width / 2 : at.x + 12;
      var top = at.y - height - gap;
      if (top < 4) { top = at.y + (kind === 'touch' ? 28 : 20); }
      tip.style.left = Math.max(4, Math.min(room - width - 4, left)) + 'px';
      tip.style.top = Math.max(4, top) + 'px';
    }

    function render(text, at, kind) {
      tip.textContent = '';
      text.lines.forEach(function (line, i) {
        var el = document.createElement(i === 0 ? 'strong' : 'span');
        el.textContent = line;
        tip.appendChild(el);
      });
      var when = document.createElement('span');
      when.className = 'when';
      when.textContent = text.when;
      tip.appendChild(when);
      tip.hidden = false;
      place(at, kind);
      shown = { point: at, kind: kind };
    }

    /* Reads the frame on screen at a container point. `kind` decides what "no rain" does: a
       resting mouse shows nothing, a deliberate long press is answered. */
    function probe(at, kind) {
      var mine = ++generation;
      var frame = source.frame(), bounds = source.bounds(), lookup = values();
      if (!frame || !bounds || !lookup) {
        tip.hidden = true;
        shown = null;
        return;
      }
      var where = target.latLngAt(at.x, at.y);
      frameImage(frame.url).then(function (image) {
        if (mine !== generation) { return; }
        var entry = reading(image, lookup, bounds, where.lat, where.lng);
        if (!entry && kind === 'mouse') {
          tip.hidden = true;
          shown = null;
          return;
        }
        render(describe(entry, frame), at, kind);
      }).catch(function () {
        // The frame could not be fetched or read: say nothing rather than something wrong.
        if (mine === generation) { tip.hidden = true; shown = null; }
      });
    }

    container.addEventListener('pointermove', function (event) {
      if (event.pointerType === 'mouse') {
        // A button held is the map being dragged, or the pin.
        if (event.buttons || !onMap(event)) { cancel(); if (shown) { hide(); } return; }
        var at = point(event);
        if (shown && !far(at, shown.point, MOUSE_SLOP_PX)) { return; }
        cancel();
        if (shown) { hide(); }
        timer = setTimeout(function () { timer = null; probe(at, 'mouse'); }, HOVER_MS);
      } else if (press && far(point(event), press, TOUCH_SLOP_PX)) {
        cancel();
        press = null;
      }
    });

    container.addEventListener('pointerleave', function (event) {
      if (event.pointerType === 'mouse') { cancel(); hide(); }
    });

    container.addEventListener('pointerdown', function (event) {
      fingers.add(event.pointerId);
      swallowClick = false;
      cancel();
      hide();
      if (event.pointerType === 'mouse' || !onMap(event)) { return; }
      // A second finger is a pinch.
      if (fingers.size > 1) { press = null; return; }
      press = point(event);
      var at = press;
      timer = setTimeout(function () {
        timer = null;
        swallowClick = true;
        probe(at, 'touch');
      }, PRESS_MS);
    });

    function lift(event) {
      fingers.delete(event.pointerId);
      cancel();
      press = null;
    }
    container.addEventListener('pointerup', lift);
    container.addEventListener('pointercancel', lift);

    // Capture, so it runs before the map's own handler, which would move the pin to this spot.
    container.addEventListener('click', function (event) {
      if (!swallowClick) { return; }
      swallowClick = false;
      event.stopPropagation();
      event.preventDefault();
    }, true);

    // Android's long-press menu ("Bild herunterladen") and iOS's callout on the Leaflet image -
    // only for a press of ours, so a right click with a mouse keeps its menu.
    container.addEventListener('contextmenu', function (event) {
      if (press || swallowClick) { event.preventDefault(); }
    });

    container.addEventListener('wheel', function () { cancel(); hide(); }, { passive: true });

    // Playing the loop or moving the slider changes the frame under a tooltip that is up.
    if (source.watch) {
      source.watch(function () {
        if (shown) { probe(shown.point, shown.kind); }
      });
    }

    return { hide: hide };
  }

  global.RainProbe = {
    attach: attach, decodeFrame: decodeFrame, pixelAt: pixelAt, lookupTable: lookupTable,
    reading: reading, describe: describe, number: number, rgbaKey: rgbaKey
  };
})(typeof window !== 'undefined' ? window : globalThis);
