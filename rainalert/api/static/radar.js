/* The map pieces the pages share.
 *
 * Before this, the radar page and `/manage` each carried their own copy of the basemap fallback
 * and the city labels, and the radar timeline lived on one page only. Putting the picker and the
 * timeline on the signup page too would have made that three copies of each, so they live here
 * instead and every page passes in its own elements.
 *
 * Leaflet is the one global assumed; everything else arrives through the options.
 */
(function (global) {
  'use strict';

  var PLACES = [
    ['Hamburg', 53.551, 9.994], ['Berlin', 52.520, 13.405], ['Hannover', 52.376, 9.732],
    ['Dortmund', 51.514, 7.466], ['Leipzig', 51.340, 12.375], ['Koeln', 50.938, 6.960],
    ['Dresden', 51.051, 13.739], ['Frankfurt', 50.110, 8.682], ['Nuernberg', 49.452, 11.077],
    ['Stuttgart', 48.776, 9.183], ['Muenchen', 48.135, 11.582], ['Bremen', 53.079, 8.802],
    ['Rostock', 54.093, 12.131], ['Freiburg', 47.999, 7.842]
  ];

  /* The marker Leaflet cannot draw for us: its default icon is a PNG fetched from wherever the
     library came from, which `img-src` does not allow and should not - a third party would learn
     the visitor's IP on every map view. Inline SVG needs no request and no CSP exception. */
  function pinIcon() {
    return L.divIcon({
      className: 'pin',             // replaces leaflet-div-icon, which is a white box
      // 20x29 is 26x38 at ~75%. The viewBox stays 0 0 26 38, so the path is untouched.
      html: '<svg viewBox="0 0 26 38" width="20" height="29" role="img"'
        + ' aria-label="Dein Standort">'
        + '<path d="M13 0C5.8 0 0 5.8 0 13c0 9.1 11.3 22.6 12.2 23.6a1 1 0 0 0 1.6 0'
        + 'C14.7 35.6 26 22.1 26 13 26 5.8 20.2 0 13 0z" fill="#1f6fb2" stroke="#fff"'
        + ' stroke-width="2"/>'
        + '<circle cx="13" cy="13" r="4.5" fill="#fff"/></svg>',
      iconSize: [20, 29],
      iconAnchor: [10, 29]          // the tip, not the middle, sits on the coordinate
    });
  }

  /* Tiles when a provider is configured, otherwise enough reference to aim by. */
  function basemap(map, opts) {
    if (opts.tileUrl) {
      L.tileLayer(opts.tileUrl, {
        // 18, not the 12 the radar's own resolution would suggest: the basemap is what you
        // orient by, and street names are the difference between "somewhere in Neuhausen" and
        // "my street".
        maxZoom: 18,
        attribution: opts.tileAttribution || '',
        // These pages send `Referrer-Policy: no-referrer`, which would strip the Referer from
        // tile requests too, and tile services read that header. The element attribute overrides
        // the document policy for these requests only, and sends the origin alone - never a path
        // or a query, so no token can ride along.
        referrerPolicy: 'strict-origin-when-cross-origin'
      }).addTo(map);
      return;
    }
    if (opts.graticule) {
      var style = { color: '#8aa', weight: 0.5, opacity: 0.5, interactive: false };
      for (var lat = 46; lat <= 56; lat++) {
        L.polyline([[lat, 3], [lat, 18]], style).addTo(map);
      }
      for (var lon = 4; lon <= 18; lon++) {
        L.polyline([[46, lon], [56, lon]], style).addTo(map);
      }
    }
    PLACES.forEach(function (p) {
      L.circleMarker([p[1], p[2]], {
        radius: 2.5, color: '#456', weight: 1, fillOpacity: 1, interactive: false
      }).addTo(map).bindTooltip(p[0], { permanent: true, direction: 'right', className: 'place' });
    });
  }

  /* A draggable pin and the circle that shows what is actually evaluated.
     `onChange(lat, lon)` fires for every move, whoever caused it. */
  function picker(map, opts) {
    var marker = null, ring = null, radius = opts.radius || 2000;

    function place(lat, lon, quiet) {
      if (marker) {
        marker.setLatLng([lat, lon]);
        ring.setLatLng([lat, lon]);
      } else {
        ring = L.circle([lat, lon], {
          radius: Math.max(radius, 50), color: '#1f6fb2', weight: 1, fillOpacity: 0.08,
          interactive: false
        }).addTo(map);
        marker = L.marker([lat, lon], { draggable: true, keyboard: true, icon: pinIcon() })
          .addTo(map);
        marker.on('dragend', function () {
          var at = marker.getLatLng();
          place(at.lat, at.lng);
        });
      }
      if (!quiet && opts.onChange) { opts.onChange(lat, lon); }
    }

    map.on('click', function (event) { place(event.latlng.lat, event.latlng.lng); });

    return {
      set: place,
      has: function () { return marker !== null; },
      setRadius: function (metres) {
        radius = metres;
        if (ring) { ring.setRadius(Math.max(metres, 50)); }
      }
    };
  }

  /* The "where am I" button, in the corner of the map where a map's own controls live.
     What it does when it finds you is the caller's business: the radar page shows the spot,
     the signup page moves the pin to it. Everything else - the chrome, the busy and found
     states, stopping the click from panning the map and jumping the page to the anchor - is
     the same on both, and was written twice before this. */
  function locateControl(opts) {
    var Ctrl = L.Control.extend({
      options: { position: 'topleft' },
      onAdd: function (map) {
        var bar = L.DomUtil.create('div', 'leaflet-bar locate-control');
        // What Leaflet's own docs prescribe for a custom control, and it matters more here than
        // on the radar page: the signup map turns a click into the subscriber's location, so a
        // click that reached it from this button would move the pin to wherever the button is.
        L.DomEvent.disableClickPropagation(bar);
        var link = L.DomUtil.create('a', '', bar);
        link.href = '#';
        link.title = 'Zu meinem Standort';
        link.setAttribute('role', 'button');
        link.setAttribute('aria-label', 'Zu meinem Standort');
        link.innerHTML = '&#9678;';        // ◎ - a target, legible at 30 px in both themes
        L.DomEvent.on(link, 'click', function (event) {
          L.DomEvent.stop(event);          // or the map pans and the page jumps to the anchor
          RainGeo.locate({
            onBusy: function (busy) { link.className = busy ? 'busy' : ''; },
            onStatus: function (text, kind) {
              if (opts.onStatus) { opts.onStatus(text, kind); }
            },
            onFound: function (lat, lon, accuracy) {
              link.className = 'on';
              opts.onFound(lat, lon, accuracy, map);
            }
          });
        });
        return bar;
      }
    });
    return new Ctrl();
  }

  /* The colour scale, as a control on the map rather than a strip under it.

     Under the map it was a row of seven swatches that every reader scrolled past on every visit
     to reach the thing below it. On the merged page there is more below it to reach - the whole
     signup form - so a permanent strip costs every visitor vertical space to explain a scale
     most of them already understand from the map itself.

     A <details>, not a div and a click handler. Open/closed state, the disclosure triangle,
     keyboard operation and the right announcement to a screen reader all come with the element;
     re-implementing that on a <div> is how a control ends up reachable by mouse only.

     Collapsed by default: the legend answers a question the reader has to have thought of.

     `stopPropagation` and not `L.DomEvent.stop`: on the signup page a map click sets the
     subscriber's location, so a click that reached the map from here would move their pin to
     wherever this control sits - but `preventDefault` on a <summary> stops it toggling, which
     would leave a legend that cannot be opened at all. */
  function legendControl(opts) {
    var options = opts || {};
    var box = null;
    var Ctrl = L.Control.extend({
      options: { position: options.position || 'bottomleft' },
      onAdd: function () {
        var wrap = L.DomUtil.create('details', 'legend-control');
        L.DomEvent.disableClickPropagation(wrap);
        L.DomEvent.disableScrollPropagation(wrap);
        var summary = L.DomUtil.create('summary', '', wrap);
        summary.textContent = 'Legende';
        box = L.DomUtil.create('div', 'legend', wrap);
        return wrap;
      }
    });
    var control = new Ctrl();
    /* The element `timeline({legend: ...})` fills. Created in `onAdd`, so this is null until the
       control is added to a map - callers add it first and read this after. */
    control.body = function () { return box; };
    return control;
  }

  /* A place the reader is being shown, rather than one they are choosing.
     Same pin and same circle as the settings map, deliberately: the two pages should not
     disagree about what a pin means. Nothing here is draggable and nothing binds a map click -
     this marks a spot, it does not pick one. */
  function mark(map, lat, lon, radius) {
    /* Removes the previous pin before drawing the new one. This used to add and keep no handle,
       which was fine while it ran once per document - a warning opened a new tab. Since a tapped
       warning now re-uses an open tab and `hashchange` re-reads the token, it runs again in
       the same document: a reader who has moved would see the old pin and the new one with no way
       to tell which is current, and every warning after that would retain another pair of layers. */
    if (map.__rainalertMark) {
      map.__rainalertMark.forEach(function (layer) { map.removeLayer(layer); });
    }
    var circle = L.circle([lat, lon], {
      radius: Math.max(radius || 0, 50), color: '#1f6fb2', weight: 1, fillOpacity: 0.08,
      interactive: false
    }).addTo(map);
    var pin = L.marker([lat, lon], { icon: pinIcon(), interactive: false, keyboard: false })
      .addTo(map);
    map.__rainalertMark = [circle, pin];
  }

  /* The radar loop. Every element is optional except the map, so a page can take the overlay
     and the stamp without the slider and the play button. */
  function timeline(map, opts) {
    var layer = null, frames = [], gaps = [], bounds = null, timer = null;
    var cache = new Map();          // url -> Image, LRU-evicted
    /* Frames held as decoded Images. 100 covers six hours outright and most of twelve, which is
       the default range.

       Not the whole range, deliberately, and the number that matters is not the one you would
       guess: a frame is 24 KB as a PNG but 1120x1361 RGBA once decoded, which is 5.8 MB. Holding
       all 600 frames of a 48-hour range would invite the browser to keep up to 3.4 GB of decoded
       bitmap, and on a phone that ends as a killed renderer rather than a slow one. 100 is about
       580 MB worst case.

       What makes a modest number sufficient is that eviction is cheap: the overlay PNGs are served
       `immutable` with a two-day max-age (storage.py), so a frame that falls out of this Map comes
       back from the browser's own HTTP cache rather than the network. This buys decode time, not
       bandwidth. */
    var MAX_CACHED = 100;
    var WINDOW = 6;                 // how far either side of the cursor to prefetch
    var slider = opts.slider, play = opts.play, stamp = opts.stamp, banner = opts.banner;
    var current = null;             // offset_minutes of the frame on screen

    /* The bubble over the knob while it is held (D-56): how far the frame under the finger is
       from the latest radar image - the same "jetzt" the stamp line uses. Above the knob because
       on a phone the finger covers the knob and the stamp line below it. aria-hidden, because
       the slider's own aria-valuetext (set in show()) already says it to a screen reader. */
    var bubble = null;
    if (slider && slider.parentNode) {
      bubble = document.createElement('span');
      bubble.className = 'slider-bubble';
      bubble.setAttribute('aria-hidden', 'true');
      bubble.hidden = true;
      slider.parentNode.appendChild(bubble);
    }
    // The browsers' default range thumb is 16-20px; the error is a couple of pixels of bubble.
    var THUMB_PX = 16;

    function placeBubble() {
      var frame = frames[+slider.value];
      if (!bubble || !frame) { return; }
      bubble.textContent = relative(frame.offset_minutes);
      bubble.hidden = false;
      var min = +slider.min || 0, max = +slider.max || 0;
      var fraction = max > min ? (+slider.value - min) / (max - min) : 0;
      var centre = slider.offsetLeft + THUMB_PX / 2 + fraction * (slider.offsetWidth - THUMB_PX);
      // Kept inside the row at both ends rather than centred on the knob and cut off.
      var width = bubble.offsetWidth, room = slider.parentNode.clientWidth;
      bubble.style.left = Math.max(0, Math.min(room - width, centre - width / 2)) + 'px';
    }

    function hideBubble() { if (bubble) { bubble.hidden = true; } }

    /* A short buzz when a drag crosses or lands on "jetzt", the line between measured and
       forecast (D-56). Only from `input`, which fires for the reader's own moves and never for
       playback or a range change, so the animation does not buzz every loop. Android browsers
       only: Safari has no vibration API and Firefox removed it, and both simply skip this. */
    function crossesNow(before, after) {
      if (before === null || before === 0) { return false; }
      return after === 0 || (after > 0) !== (before > 0);
    }

    // 30 ms, not the 12 it was: a phone's vibration motor needs a moment to spin up, and on many
    // a pulse much shorter than ~20 ms is too weak to feel at all.
    function buzz() {
      try {
        if (navigator.vibrate) { navigator.vibrate(30); }
      } catch (e) { /* not worth a broken slider */ }
    }

    function say(text) {
      if (!banner) { return; }
      banner.hidden = false;
      banner.textContent = text;
    }

    function preload(url) {
      if (cache.has(url)) { return cache.get(url); }
      var img = new Image();
      img.src = url;
      cache.set(url, img);
      while (cache.size > MAX_CACHED) { cache.delete(cache.keys().next().value); }
      return img;
    }

    function gapAt(offset) {
      return gaps.some(function (g) {
        return offset >= g.from_offset_minutes && offset <= g.to_offset_minutes;
      });
    }

    // Minutes up to an hour and a half, hours beyond it. The slider reaches twelve hours back,
    // where "-720 min" is a number you have to divide before it means anything.
    function relative(minutes) {
      if (minutes === 0) { return 'jetzt'; }
      var sign = minutes < 0 ? '-' : '+';
      var abs = Math.abs(minutes);
      if (abs <= 90) { return sign + abs + ' min'; }
      return sign + Math.floor(abs / 60) + ':' + String(abs % 60).padStart(2, '0') + ' h';
    }

    function label(frame) {
      var when = new Date(frame.valid_time);
      var time = when.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
      // The date is always shown, not only when the window crosses midnight. A label that gains
      // a date only sometimes is one you have to read twice to be sure it has not.
      var day = when.toLocaleDateString('de-DE', {
        weekday: 'short', day: '2-digit', month: '2-digit'
      });
      var kind = frame.kind === 'forecast'
        ? '<span class="future">Vorhersage</span>'
        : '<span class="kind">gemessen</span>';
      return '<span class="day">' + day + '</span> ' + time + ' Uhr · '
        + relative(frame.offset_minutes) + ' · ' + kind;
    }

    function show(index) {
      var frame = frames[index];
      if (!frame) { return; }
      current = frame.offset_minutes;
      if (stamp) { stamp.innerHTML = label(frame); }
      if (slider) { slider.setAttribute('aria-valuetext', relative(frame.offset_minutes)); }

      if (gapAt(frame.offset_minutes)) {
        // Deliberately blank rather than holding the previous image: pretending the radar saw
        // something it did not is exactly the failure this service must not have.
        if (layer) { map.removeLayer(layer); layer = null; }
        if (stamp) { stamp.innerHTML += ' · <span class="kind">keine Daten</span>'; }
        return;
      }
      preload(frame.url);
      if (layer) {
        layer.setUrl(frame.url);
      } else {
        // The alpha is baked into the PNG (radar/overlay.py INTENSITY_BANDS), so this is 1.0
        // and the palette is the single place that decides how strong rain looks.
        layer = L.imageOverlay(frame.url, bounds, { opacity: opts.layerOpacity }).addTo(map);
        // Under the pin, or the radar hides the thing being positioned.
        if (opts.behindMarkers) { layer.bringToBack(); }
      }
      if (!layer._map) { layer.addTo(map); }
      for (var d = -WINDOW; d <= WINDOW; d++) {
        var neighbour = frames[index + d];
        if (neighbour && !gapAt(neighbour.offset_minutes)) { preload(neighbour.url); }
      }
    }

    // Playback speed. 500 ms is the middle of the range that works: below roughly 300 ms the eye
    // has no time to fixate on where a shower is relative to a town, and above about 700 ms the
    // frames stop reading as one moving thing and become a slideshow.
    var STEP_MS = 500;
    // Two places deserve longer than a step. The end, so the last forecast frame - the answer to
    // "will it reach me" - is still on screen when you look back at it. And t+0, where
    // measurement stops and prediction starts.
    var HOLD_LAST_MS = 1800;
    var HOLD_NOW_MS = 1000;

    function stop() {
      if (timer) { clearTimeout(timer); timer = null; if (play) { play.textContent = '▶'; } }
    }

    if (play) {
      play.addEventListener('click', function () {
        if (timer) { stop(); return; }
        hideBubble();
        play.textContent = '❚❚';
        var start = frames.findIndex(function (f) { return f.offset_minutes >= -60; });
        if (+slider.value >= frames.length - 1) { slider.value = Math.max(0, start); }

        // A self-scheduling timeout rather than setInterval, so a frame can be held longer than
        // a step. setInterval cannot vary its period, and it stacks callbacks if a frame is slow
        // to paint, which on a phone turns a pause into a stutter and then a jump.
        function hold(index) {
          if (index >= frames.length - 1) { return HOLD_LAST_MS; }
          if (frames[index] && frames[index].offset_minutes === 0) { return HOLD_NOW_MS; }
          return STEP_MS;
        }

        function tick() {
          var next = +slider.value + 1;
          if (next >= frames.length) { next = Math.max(0, start); }
          slider.value = next;
          show(next);
          timer = setTimeout(tick, hold(next));
        }
        timer = setTimeout(tick, hold(+slider.value));
      });
    }

    if (slider) {
      slider.addEventListener('input', function () {
        var before = current;
        stop();
        show(+slider.value);
        placeBubble();
        if (crossesNow(before, current)) { buzz(); }
      });
      // Shown while pressed, from the first touch rather than the first move, and gone on
      // release. On the window, because the release can happen anywhere once the finger slides
      // off the slider. Keyboard moves show it through `input` and hide it on blur.
      slider.addEventListener('pointerdown', placeBubble);
      window.addEventListener('pointerup', hideBubble);
      window.addEventListener('pointercancel', hideBubble);
      slider.addEventListener('blur', hideBubble);
    }

    /* The fetch is a function rather than a statement, so the range picker can ask for a new
       window on a live timeline.

       Calling `timeline()` again would be the obvious alternative and is wrong: the slider and
       play listeners are registered above, once per call, so a second call leaves two `input`
       handlers on the slider and two `click` handlers on the button - and the second play tap
       then starts two self-scheduling timers that fight over `slider.value`. Reloading in place
       keeps exactly one of each.

       The image cache is deliberately *not* cleared. It is keyed by URL and the ranges nest -
       everything in 6 h is also in 12 h - so a reader stepping down the ranges re-shows frames
       that are already decoded. */
    function load(pastHours) {
      stop();
      if (slider) { slider.disabled = true; }
      if (banner) { banner.hidden = true; }
      return fetch('/api/v1/overlays/timeline?past_hours=' + encodeURIComponent(pastHours))
        .then(function (r) {
          // `fetch` does not reject on 4xx/5xx, so without this the error body flows on and
          // `data.frames` is undefined - which threw, killed the script, and left the page with a
          // dead slider and no explanation. "Overlays are not configured" is the normal state of a
          // fresh install, and it has a message of its own to show.
          if (!r.ok) { return { frames: [] }; }
          return r.json();
        })
        .then(function (data) {
          frames = Array.isArray(data.frames) ? data.frames : [];
          gaps = data.gaps || [];
          bounds = data.bounds;

          if (!frames.length || !bounds) {
            // Removed, not left behind: on a reload the map would otherwise keep showing a frame
            // from the window we just navigated away from, with nothing on the slider to match it.
            if (layer) { map.removeLayer(layer); layer = null; }
            say('Noch keine Radardaten vorhanden.');
            if (opts.onEmpty) { opts.onEmpty(); }
            return;
          }
          if (data.stale) {
            say('Die Radardaten sind ' + Math.round(data.age_minutes)
              + ' Minuten alt – das Bild zeigt nicht die aktuelle Lage.');
          }
          if (slider) {
            slider.max = frames.length - 1;
            slider.disabled = false;
          }
          var nowIndex = frames.findIndex(function (f) { return f.offset_minutes === 0; });
          var at = nowIndex < 0 ? 0 : nowIndex;
          if (slider) { slider.value = at; }
          show(at);
          if (opts.legend) {
            // The band names go in the title rather than inline: seven of them spelled out wraps
            // to four lines on a phone and competes with the map for the screen. The settings page
            // spells them out, because there the name is what you are choosing.
            opts.legend.innerHTML = (data.colorscale || []).map(function (s) {
              var c = s.rgba;
              var name = s.label ? s.label + ' – ab ' + s.from_mm_5min + ' mm/5 min (~'
                + s.approx_mm_per_hour + ' mm/h)' : '';
              return '<span title="' + name + '"><i style="background:rgba(' + c[0] + ',' + c[1]
                + ',' + c[2] + ',' + (c[3] / 255).toFixed(2) + ')"></i>'
                + s.from_mm_5min.toFixed(2) + '</span>';
            }).join('') + '<span>mm / 5 min</span>';
          }
          if (opts.onReady) { opts.onReady(frames[at]); }
        })
        .catch(function () {
          say('Die Radardaten konnten nicht geladen werden.');
          if (opts.onEmpty) { opts.onEmpty(); }
        });
    }

    load(opts.pastHours);

    return { stop: stop, show: show, load: load };
  }

  global.RainRadar = {
    basemap: basemap, picker: picker, timeline: timeline, pinIcon: pinIcon, mark: mark,
    locateControl: locateControl, legendControl: legendControl
  };
})(window);
