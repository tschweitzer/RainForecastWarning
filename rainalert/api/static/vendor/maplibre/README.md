# MapLibre GL JS 6.13.0, vendored

Draws the maps on the start and settings pages (DESIGN.md D-58, the default since D-59). Leaflet
(`../leaflet/`) stays as the fallback where MapLibre cannot run.

Served from this app rather than a CDN, for the reason in `../leaflet/README.md`: a CDN would
learn every visitor's IP before the map draws.

## Provenance

Byte-identical to the files in the official npm tarball:

    https://registry.npmjs.org/maplibre-gl/-/maplibre-gl-6.13.0.tgz
    integrity  sha512-ELXk4h+xl0URFQEnVbAN6MGn61P6jq4amUWHiskLVsvHCaKdf1Zq6G8UNOD0AQ3UaXoL5ECc8ovz8yqYEBGPKw==

The checksum was verified against the registry's own metadata at the time of vendoring.
`tests/test_vendored_maplibre.py` pins the sha256 of each file, so a local edit fails the suite.
**Do not edit these files**; map behaviour lives in `../../radar-gl.js`.

## What is here

- `maplibre-gl.mjs` - the library, an ES module (MapLibre 6 ships no classic-script build).
- `maplibre-gl-worker.mjs` - its web worker. The library computes the worker's URL from its own
  (`./maplibre-gl-worker.mjs`, next to it), and because that is same-origin it starts it as a module
  worker directly - no `blob:` URL, so the CSP's `worker-src 'self'` is enough.
- `maplibre-gl.css`, `LICENSE.txt` (BSD-3-Clause).

Not vendored: the `-dev` builds and the `.map` source maps. The library's last line names its
source map; browsers only fetch it with the developer tools open, and then get a 404.
