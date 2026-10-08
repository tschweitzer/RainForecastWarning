/* The vector map: the start and settings pages' maps drawn by MapLibre on OpenStreetMap vector
 * tiles (DESIGN.md D-58, the default since D-59).
 *
 * The same functions as `RainRadar` in radar.js - createMap, basemap, picker, mark, timeline,
 * locateControl, legendControl - so signup.js picks one set and does not care which. The radar
 * loop itself is not duplicated: `timeline` is radar.js's, handed an overlay that draws through
 * MapLibre instead of Leaflet.
 *
 * What the switch is for, and the reason the radar goes where it goes in the layer stack: on a
 * raster basemap the place names are baked into the tiles, so the rain is painted over them. Here
 * the radar is inserted below the first label layer, so a town stays readable under a shower.
 *
 * Zoom levels: MapLibre's are one lower than Leaflet's for the same scale (512px tiles against
 * 256px), and signup.js speaks Leaflet's - "12 to see your street". The adapter converts, in one
 * place, so the callers' numbers mean the same on both maps.
 */
import { Map as GLMap, Marker, NavigationControl } from 'maplibre-gl';

const ZOOM_OFFSET = 1;          // Leaflet zoom = MapLibre zoom + 1
const RING_COLOUR = '#1f6fb2';  // the picker's and the settings page's circle

function webglAvailable() {
  try {
    const canvas = document.createElement('canvas');
    return !!(canvas.getContext('webgl2') || canvas.getContext('webgl'));
  } catch (e) {
    return false;
  }
}

/* The map, wrapped in the handful of Leaflet-shaped calls signup.js makes. */
function createMap(id, opts) {
  // The start page fits Germany; the settings page opens on a place and a zoom (Leaflet's scale).
  const view0 = opts.bounds
    ? { bounds: [[opts.bounds[0][1], opts.bounds[0][0]], [opts.bounds[1][1], opts.bounds[1][0]]],
        fitBoundsOptions: { padding: opts.padding || 0 } }
    : { center: [opts.center[1], opts.center[0]], zoom: opts.zoom - ZOOM_OFFSET };
  // The colour scheme the page itself follows (base.html), chosen once at load. Switching the
  // style live when the system theme changes would rebuild every layer, the radar included.
  const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  const gl = new GLMap({
    container: id,
    style: '/map-style/' + (dark ? 'gray-dark' : 'gray') + '.json',
    ...view0,
    maxZoom: 18 - ZOOM_OFFSET,
    // North stays up. A rain radar read at an angle is a radar read wrong, and a two-finger
    // twist is easy to make by accident while zooming.
    dragRotate: false,
    pitchWithRotate: false,
    touchPitch: false,
    attributionControl: { compact: true },
    /* The pages send `Referrer-Policy: no-referrer`, and tile servers read the Referer - the
       OSMF's usage policies ask for one. The same exception the Leaflet tiles make (radar.js
       `basemap`): the origin alone, never a path or a query, and only to the tile server. */
    transformRequest: (url, resourceType) =>
      resourceType === 'Tile' ? { url, referrerPolicy: 'strict-origin-when-cross-origin' } : { url }
  });
  gl.touchZoomRotate.disableRotation();
  gl.keyboard.disableRotation();
  gl.addControl(new NavigationControl({ showCompass: false }), 'top-left');

  // Sources and layers can only be added once the style has loaded; anything asked for before
  // then waits here, in order.
  let loaded = false;
  const waiting = [];
  gl.on('load', () => {
    loaded = true;
    waiting.splice(0).forEach((fn) => fn());
  });

  const view = {
    gl,
    ready(fn) { if (loaded) { fn(); } else { waiting.push(fn); } },
    setView(latlng, zoom) {
      gl.jumpTo({ center: [latlng[1], latlng[0]], zoom: zoom - ZOOM_OFFSET });
      return view;
    },
    getZoom() { return gl.getZoom() + ZOOM_OFFSET; },
    addControl(control) {
      gl.addControl(control, control.position || 'top-left');
      return view;
    }
  };
  return view;
}

/* Nothing to do: the style is the basemap. Here so the two engines have the same functions. */
function basemap() {}

function pinElement() {
  const el = document.createElement('div');
  el.className = 'pin';
  el.innerHTML = window.RainRadar.PIN_SVG;
  return el;
}

/* The circle around a pin: a GeoJSON polygon in a source of its own, drawn above everything
   else - it is added after the radar and the labels, which is where Leaflet draws it too. */
function ring(view, id) {
  const source = id + '-ring';
  let pending = null;

  function polygon(lat, lon, metres) {
    const points = [];
    const dLat = metres / 111320;
    const dLon = metres / (111320 * Math.cos(lat * Math.PI / 180));
    for (let i = 0; i <= 64; i++) {
      const a = (i / 64) * 2 * Math.PI;
      points.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
    }
    return { type: 'Feature', properties: {}, geometry: { type: 'Polygon', coordinates: [points] } };
  }

  return {
    set(lat, lon, metres) {
      pending = polygon(lat, lon, metres);
      view.ready(() => {
        const gl = view.gl;
        if (gl.getSource(source)) {
          gl.getSource(source).setData(pending);
          return;
        }
        gl.addSource(source, { type: 'geojson', data: pending });
        gl.addLayer({ id: source + '-fill', type: 'fill', source,
          paint: { 'fill-color': RING_COLOUR, 'fill-opacity': 0.08 } });
        gl.addLayer({ id: source + '-line', type: 'line', source,
          paint: { 'line-color': RING_COLOUR, 'line-width': 1 } });
      });
    }
  };
}

/* A draggable pin and the circle that shows what is evaluated. Same contract as radar.js:
   `onChange(lat, lon)` fires for every move, whoever caused it, except `set(..., quiet)`. */
function picker(view, opts) {
  let marker = null;
  let radius = opts.radius || 2000;
  const circle = ring(view, 'pick');

  function place(lat, lon, quiet) {
    if (marker) {
      marker.setLngLat([lon, lat]);
    } else {
      marker = new Marker({ element: pinElement(), draggable: true, anchor: 'bottom' })
        .setLngLat([lon, lat]).addTo(view.gl);
      marker.on('dragend', () => {
        const at = marker.getLngLat();
        place(at.lat, at.lng);
      });
    }
    circle.set(lat, lon, Math.max(radius, 50));
    if (!quiet && opts.onChange) { opts.onChange(lat, lon); }
  }

  view.gl.on('click', (event) => {
    /* Not a click on the pin itself. MapLibre puts markers inside the map's own container, so a
       tap on the pin reaches this handler too - at the tap's coordinates, which are above the
       pin's tip - and every tap on the pin moved it about 30 km north at country zoom. Leaflet
       stops marker clicks before the map sees them; here it has to be said. */
    const target = event.originalEvent && event.originalEvent.target;
    if (target && target.closest && target.closest('.maplibregl-marker')) { return; }
    place(event.lngLat.lat, event.lngLat.lng);
  });

  return {
    set: place,
    has: () => marker !== null,
    setRadius(metres) {
      radius = metres;
      if (marker) {
        const at = marker.getLngLat();
        circle.set(at.lat, at.lng, Math.max(metres, 50));
      }
    }
  };
}

/* A place being shown rather than chosen: the same pin and circle, nothing interactive, and a
   second call replaces the first rather than adding to it (radar.js `mark` says why). */
function mark(view, lat, lon, radius) {
  if (!view.marked) {
    view.marked = { marker: null, circle: ring(view, 'mark') };
  }
  if (view.marked.marker) { view.marked.marker.remove(); }
  view.marked.marker = new Marker({ element: pinElement(), anchor: 'bottom' })
    .setLngLat([lon, lat]).addTo(view.gl);
  view.marked.circle.set(lat, lon, Math.max(radius || 0, 50));
}

/* The "where am I" button, as a MapLibre control. Behaviour as radar.js `locateControl`. */
function locateControl(opts) {
  let bar = null;
  return {
    position: 'top-left',
    onAdd() {
      bar = document.createElement('div');
      bar.className = 'maplibregl-ctrl maplibregl-ctrl-group locate-control';
      const link = document.createElement('a');
      link.href = '#';
      link.title = 'Zu meinem Standort';
      link.setAttribute('role', 'button');
      link.setAttribute('aria-label', 'Zu meinem Standort');
      link.innerHTML = '&#9678;';
      link.addEventListener('click', (event) => {
        // Not the map's click: on the signup page that would move the pin to the button.
        event.preventDefault();
        event.stopPropagation();
        window.RainGeo.locate({
          onBusy: (busy) => { link.className = busy ? 'busy' : ''; },
          onStatus: (text, kind) => { if (opts.onStatus) { opts.onStatus(text, kind); } },
          onFound: (lat, lon, accuracy) => {
            link.className = 'on';
            opts.onFound(lat, lon, accuracy);
          }
        });
      });
      bar.appendChild(link);
      return bar;
    },
    onRemove() { if (bar) { bar.remove(); } }
  };
}

/* The colour scale in a <details>, as a MapLibre control. radar.js `legendControl` explains the
   element and why it is collapsed. */
function legendControl() {
  let wrap = null;
  let box = null;
  return {
    position: 'bottom-left',
    onAdd() {
      wrap = document.createElement('details');
      wrap.className = 'maplibregl-ctrl legend-control';
      const summary = document.createElement('summary');
      summary.textContent = 'Legende';
      box = document.createElement('div');
      box.className = 'legend';
      wrap.append(summary, box);
      return wrap;
    },
    onRemove() { if (wrap) { wrap.remove(); } },
    body: () => box
  };
}

/* The radar as an image source, placed under the first label layer of the style. */
function overlay(view, opts) {
  const opacity = isFinite(opts.layerOpacity) ? opts.layerOpacity : 1;

  /* Where the radar goes: just under the labels. The VersaTiles styles carry an empty layer
     named for exactly that; failing it, the first label layer does the same job. */
  function firstLabelLayer(gl) {
    if (gl.getLayer('slot-below-labels')) { return 'slot-below-labels'; }
    const label = gl.getStyle().layers.find((layer) => layer.type === 'symbol');
    return label ? label.id : undefined;
  }

  return {
    show(url, bounds) {
      const [[south, west], [north, east]] = bounds;
      const coordinates = [[west, north], [east, north], [east, south], [west, south]];
      view.ready(() => {
        const gl = view.gl;
        const source = gl.getSource('radar');
        if (source) {
          source.updateImage({ url, coordinates });
        } else {
          gl.addSource('radar', { type: 'image', url, coordinates });
          gl.addLayer({
            id: 'radar', type: 'raster', source: 'radar',
            paint: {
              'raster-opacity': opacity,
              // No cross-fade between frames: the loop is a sequence of measurements, and a blend
              // of two of them is a picture of rain that was never there.
              'raster-fade-duration': 0,
              // Blocky when zoomed in, like the cells are (radar/overlay.py: a smoothed edge is a
              // worse lie than a blocky one).
              'raster-resampling': 'nearest'
            }
          }, firstLabelLayer(gl));
        }
        gl.setLayoutProperty('radar', 'visibility', 'visible');
      });
    },
    hide() {
      view.ready(() => {
        if (view.gl.getLayer('radar')) { view.gl.setLayoutProperty('radar', 'visibility', 'none'); }
      });
    }
  };
}

function timeline(view, opts) {
  return window.RainRadar.timeline(view, Object.assign({}, opts, { overlay: overlay(view, opts) }));
}

if (webglAvailable() && window.RainRadar) {
  window.RainRadarGL = {
    createMap, basemap, picker, mark, timeline, locateControl, legendControl
  };
}
