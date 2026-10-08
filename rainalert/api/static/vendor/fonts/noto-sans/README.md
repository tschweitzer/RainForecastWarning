# Noto Sans, vendored

The label font of the vector map trial (DESIGN.md D-58), declared in the map styles' `font-faces`
(`../../map/*.json`, built by `scripts/map-style/build.mjs`). Self-hosted so that drawing a town
name does not mean asking a third-party glyph server.

From the npm package `@fontsource/noto-sans@5.3.0` (SIL Open Font License 1.1, `LICENSE`):

    https://registry.npmjs.org/@fontsource/noto-sans/-/noto-sans-5.3.0.tgz
    integrity  sha512-fBCog2PY7DiVVTEEqtI/Qdinx/knobHYfoGpjFXdfBX5RoJaBp1Prw2G75p0OIsdlMZg3cLo0c+YbIUYOxnQJw==

Only the `latin` and `latin-ext` subsets, regular (400) and bold (700): enough for German and
for the place names of every neighbouring country, which is what the map shows. A label in another
script falls back to the browser's own fonts.
