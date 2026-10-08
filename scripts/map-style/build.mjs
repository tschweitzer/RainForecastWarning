/* Builds the two basemap styles the vector map trial uses (DESIGN.md D-58):

     cd scripts/map-style && npm ci && npm run build

   Writes rainalert/api/static/map/gray.json and gray-dark.json. The output is committed; this only
   runs when the style should change.

   The styles come from VersaTiles (MIT), whose `gray` palette was chosen because the radar is the
   thing to look at: a colourful basemap competes with the rain colours, and the `mäßiger Regen`
   green disappears over green landcover (DESIGN.md §11.1.1). Built for the Shortbread schema, which
   is what vector.openstreetmap.org serves. What is changed after generating, and why: */
import { osm } from '@versatiles/style';
import { mkdirSync, writeFileSync } from 'node:fs';

const OUT = new URL('../../rainalert/api/static/map/', import.meta.url);

// Rewritten by the app when it serves the style (`/map-style/<theme>.json`), so the tile server is
// a setting rather than something baked into a committed file.
const TILES = '{vector_tile_url}';

// Self-hosted label fonts through the style's `font-faces` (MapLibre 6). The generated style
// points at a glyph server on tiles.versatiles.org instead - a third party every visitor's browser
// would contact, which is what vendoring Leaflet removed (static/vendor/leaflet/README.md). The
// URLs are static-file names; the app turns them into versioned URLs (assets.py).
const LATIN = ['U+0000-00FF', 'U+0131', 'U+0152-0153', 'U+02BB-02BC', 'U+02C6', 'U+02DA', 'U+02DC',
  'U+0304', 'U+0308', 'U+0329', 'U+2000-206F', 'U+20AC', 'U+2122', 'U+2191', 'U+2193', 'U+2212',
  'U+2215', 'U+FEFF', 'U+FFFD'];
const LATIN_EXT = ['U+0100-02BA', 'U+02BD-02C5', 'U+02C7-02CC', 'U+02CE-02D7', 'U+02DD-02FF',
  'U+0304', 'U+0308', 'U+0329', 'U+1D00-1DBF', 'U+1E00-1E9F', 'U+1EF2-1EFF', 'U+2020',
  'U+20A0-20AB', 'U+20AD-20C0', 'U+2113', 'U+2C60-2C7F', 'U+A720-A7FF'];
const face = (weight) => [
  { url: `vendor/fonts/noto-sans/noto-sans-latin-${weight}-normal.woff2`, 'unicode-range': LATIN },
  { url: `vendor/fonts/noto-sans/noto-sans-latin-ext-${weight}-normal.woff2`, 'unicode-range': LATIN_EXT },
];

mkdirSync(OUT, { recursive: true });
for (const theme of ['gray', 'gray-dark']) {
  const style = osm({
    theme,
    text: { language: 'de' },
    // Flat. The default is a globe, which at the zoom levels of a rain radar is a flat map with
    // extra work - and the radar image is laid out in Web Mercator.
    projection: 'mercator',
    urls: { osm: TILES },
  });

  // No glyph server (fonts come from `font-faces`) and no sprite: the sprite is the icon sheet for
  // shops, stations and road shields, which a rain map has no use for and which would be a second
  // third-party fetch. The layers that draw icons go with it.
  delete style.glyphs;
  delete style.sprite;
  delete style.sky;
  // Hatched areas (construction sites and the like) draw their hatching from the same sprite, so
  // they go too rather than asking for images that are not there.
  style.layers = style.layers.filter((layer) =>
    !(layer.layout && 'icon-image' in layer.layout)
    && !(layer.paint && ('fill-pattern' in layer.paint || 'line-pattern' in layer.paint)));
  style['font-faces'] = { noto_sans_regular: face(400), noto_sans_bold: face(700) };

  for (const source of Object.values(style.sources)) {
    if (source.type !== 'vector') { continue; }
    // The placeholder has no `{z}`, so the generator took it for a TileJSON address and resolved
    // it against its own server. A plain tile template is what is meant, and nothing else.
    delete source.url;
    source.tiles = [TILES];
    // Shortbread stops at zoom 14; without this MapLibre asks for z15+ tiles that do not exist and
    // the map goes blank when zoomed in, instead of overzooming the z14 tiles.
    source.maxzoom = 14;
    // The licence's attribution, shown in the map's corner.
    source.attribution = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>';
  }
  style.metadata = { ...style.metadata, 'rainalert:built-by': 'scripts/map-style/build.mjs' };

  writeFileSync(new URL(`${theme}.json`, OUT), JSON.stringify(style, null, 1) + '\n');
  const symbols = style.layers.filter((l) => l.type === 'symbol').map((l) => l.id);
  console.log(`${theme}: ${style.layers.length} layers, ${symbols.length} symbol layers`);
}
