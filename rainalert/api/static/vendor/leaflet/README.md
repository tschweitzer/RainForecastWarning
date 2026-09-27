# Leaflet 1.9.4, vendored

Served from this app rather than from a CDN. Until 2026-09-27 the three map pages loaded
`leaflet.js` and `leaflet.css` from `unpkg.com`, which meant every visitor to `/`, `/map` and
`/manage` announced their IP to a third party before the map drew anything — hard to defend for a
service whose privacy story is data minimisation, and the reason `MAP_SCRIPT_SRC` had to sit in
`script-src` and `style-src`. That origin is gone: the CSP is back to `'self'` for both.

## Provenance

These files are **byte-identical** to the official npm tarball, which is also what unpkg serves:

    https://registry.npmjs.org/leaflet/-/leaflet-1.9.4.tgz
    sha1       23fae724e282fa25745aff82ca4d394748db7d8d
    integrity  sha512-nxS1ynzJOmOlHp+iL3FyWqK89GtNL8U8rvlMOsQdTTssxZwCXh8N2NB3GDQOL+YR3XnWyZAxwQixURb+FA74PA==

Both checksums were verified against the registry's own metadata for `leaflet@1.9.4` at the time
of vendoring. `tests/test_vendored_leaflet.py` pins the sha256 of each file taken from that
tarball, so a local edit — an accidental reformat, an editor stripping a final newline, anything —
fails the suite instead of quietly becoming a fork nobody can diff against upstream.

    leaflet.js   db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a
    leaflet.css  a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6

**Do not edit these files.** To change anything about the map, change `../../radar.js` or the
templates. To move to a new Leaflet version, re-vendor (below) and update the hashes in that test
in the same commit.

## What is here, and what is not

`leaflet.css` refers to three images by relative path, so they have to sit in `images/` next to
it, and they do. Nothing else from `dist/` is needed:

- **`images/marker-icon.png`** is referenced by the CSS but never actually drawn. The map pin is
  an inline-SVG `L.divIcon` in `radar.js` precisely so that no raster icon is fetched from
  anywhere (`tests/test_pages.py::test_the_marker_icon_is_inline_and_needs_no_network`). It is
  vendored only so the stylesheet's `url()` resolves instead of 404ing.
- **`marker-shadow.png` and `marker-icon-2x.png` are deliberately absent.** The stylesheet does
  not reference them and the default icon is unused, so shipping them would be two files that
  exist to be fetched by nothing.
- **`leaflet.js.map` is deliberately absent.** Its `sources` list 78 files under `src/` with no
  `sourcesContent`, so it cannot reconstruct anything without also vendoring the entire Leaflet
  source tree. 225 KB that resolves to nothing is worse than the alternative, which is that
  `leaflet.js` keeps its trailing `//# sourceMappingURL=leaflet.js.map` comment and a browser
  with devtools open logs one 404 for it. Users never see that; keeping the file byte-identical
  to upstream is worth more than silencing it.

## Re-vendoring

    make vendor-leaflet

That target downloads the tarball, checks it against the sha512 above, and refreshes the six
files. It fails rather than writing anything if the checksum does not match. After bumping the
version, update the hashes here and in `tests/test_vendored_leaflet.py`.

## Licence

Leaflet is BSD-2-Clause. `LICENSE` is the upstream copy and must travel with these files — that
is what the licence asks for, and it is why the file is here rather than summarised in a comment.
