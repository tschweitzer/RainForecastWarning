# RainAlert — Design & Requirements

**Status:** Draft v2, ready for implementation
**Date:** 2026-09-16
**Changes since v1:** timeline slider extended to −12 h … +2 h (D-22); evaluation storage reduced to a
48 h debug log (D-23, §8.1); "simplicity over optimization" added as a governing principle (§1); the
RV format verified against real data (M0) and the design corrected accordingly; an adversarial
security review folded in (§4.3.1, §8.0, §9, §15, §18.1); database resolved to Cloud SQL (§6.3)
**Repository:** RainForecastWarning — the Python package/module namespace is `rainalert`
**Audience:** the coding agent that will implement this, and future-me

> **Repository.** `github.com/tschweitzer/RainForecastWarning`. This document is the authoritative
> specification until code exists; keep it updated as decisions change.

---

## 1. Problem statement

Warn a user shortly *before* it starts raining at their location in Germany, by email, using DWD
radar nowcast data. "Get the laundry in / take a jacket" — not a general weather app.

### In scope for v1
- Server-side service, deployed to Google Cloud, that ingests DWD radar nowcast data every 5 minutes.
- Email subscription with double opt-in, one location per subscriber, location updatable via API.
- Alert when rain is predicted to start at the subscriber's location within the next 30 minutes.
- Minimal server-rendered web UI: subscribe (map picker), confirm, manage, unsubscribe.
- The map picker page renders a rain **timeline**: an overlay with a slider spanning the last
  **12 hours of observed rain** through the next **2 hours of nowcast**.
- REST API shaped so a future mobile app can use it unchanged.

### Explicitly out of scope for v1
- Mobile apps (Android/iOS) and push notifications (FCM/APNs). A `Notifier` interface exists; the
  push adapter is a stub that raises `NotImplementedError`.
- Countries other than Germany / data sources other than DWD.
- User accounts with passwords, OAuth, multi-tenancy, teams.
- Snow/hail/thunderstorm classification, wind, temperature.
- "All clear" / "rain has stopped" notifications.
- Public launch paperwork (Impressum, full Datenschutzerklärung, DPA). See §13.

### Guiding principle

**Simplicity over optimization.** Where a simpler design and a cleverer one both work at the scale
this service actually operates at (tens of subscribers, one radar cycle every five minutes), take the
simpler one — fewer tables, fewer columns, fewer moving parts — even if it leaves performance or
completeness on the table. Optimize when a measurement says to, not in advance. This rule outranks
any efficiency argument elsewhere in this document.

### Non-goals / anti-requirements
- Do **not** mirror or re-publish DWD bulk data.
- Do **not** poll DWD per subscriber. One fetch per cycle, globally.
- Do **not** build an SPA or a build toolchain for the frontend.

---

## 2. Decisions log

Decisions taken during the requirements interview. Each is binding unless superseded.

| # | Decision | Rationale / note |
|---|---|---|
| D-1 | Own new repository: `tschweitzer/RainForecastWarning` | Created 2026-09-16; this doc is its first commit |
| D-2 | Trigger = "rain starts within lead window **and** it is currently dry at the location" | "Get the laundry in" semantics; avoids firing during ongoing rain |
| D-3 | Spatial rule = max over cells within a radius (default **2000 m**) | 1 km grid + nowcast advection error; a single cell is too jittery |
| D-4 | Host on GCP, serverless, scale-to-zero | Cloud Run service + Cloud Run job + Cloud Scheduler + GCS |
| D-5 | Identity = **(channel, address)** + magic link (double opt-in); subscription holds an opaque API token | GDPR consent trail, no passwords, reusable by the future app. Revised 2026-09-19: email was never the point of double opt-in - proving the channel reaches the person who asked was - so channel and address became the identity and the confirmation goes out over whatever channel that is. One code path, no special case |
| D-6 | Ingestion stores **full grids** (the original archives), not just samples | Enables replay, debugging, and the map overlay feature |
| D-7 | Retention: raw archives **48 h**; forecast overlays **1 h**; observed overlays **14 h**; `evaluations` **48 h**; `rain_events` / `notifications` indefinitely | See D-23 |
| D-8 | Alert de-duplication via a **per-subscription state machine** (§9), not a fixed cooldown | One mail per rain *event*, not per cycle |
| D-9 | v1 throttling: **none the subscriber can turn off, except a hard ceiling** — `min_gap_minutes` and quiet hours stay off by default (D-10), so a normal subscriber still gets maximum notifications for debugging, but `alert_cap_per_subscription_per_day` (12) and `global_alert_cap_per_day` (300) bound it | Revised 2026-10-04. The original read "none beyond the state machine", which stopped being true and had stopped being safe: the state machine limits one alert per dry→warned *event*, and a rule of `threshold=0.01, lead=120, radius=20000` — in spec on every axis — widens "event" until it means most cycles in unsettled weather. What that spends is shared (SECURITY_REVIEW.md F-15), so the ceiling is not the subscriber's to set |
| D-10 | `min_gap_minutes` ("only once per N minutes") and quiet hours exist in the schema and config now, default **off** (`0` / disabled) | Future-configurable without migration |
| D-11 | Language: **Python everywhere** (FastAPI + Jinja2 templates, numpy) | Radar tooling is Python; one image, one language |
| D-12 | Mail via a pluggable `Notifier`; default adapter a transactional provider (Brevo/Mailgun/SendGrid free tier); console adapter for dev | Deliverability; swappable via config |
| D-13 | Defaults: **lead time 30 min**, threshold **0.15 mm / 5 min** | Nowcast skill decays fast. The threshold was 0.1 (≈1.2 mm/h, "you get wet") until 2026-09-21; it moved to 0.15 (≈1.8 mm/h, *leichter Regen*) when the settings page became a picker of the §11.1.1 bands, because a default that is not one of the bands shows up as "eigener Wert" - a confusing first impression for something nobody chose. The band boundary, not the round number, is what makes it legible |
| D-14 | Alert rule parameters are **per-subscription columns with defaults**, not constants | v1 UI shows defaults only; later UI edits the same fields |
| D-15 | API-first; push is a stubbed adapter | No FCM work in v1 |
| D-16 | One subscriber (identified by email) → **one subscription** → **one location**, updatable | See D-17 for the consequence |
| D-17 | A location change of more than 1 km resets the alert state to `UNKNOWN` | Otherwise moving into existing rain produces a bogus "rain starting" mail |
| D-18 | Audience: private (me + friends); designed so going public later is a config/paperwork change, not a rewrite | Still: double opt-in, one-click unsubscribe, deletion endpoint |
| D-19 | Frontend: server-rendered HTML, no build step; Leaflet for the map, basemap tiles from a configured provider or none (§11.1). *Since D-59 the maps are drawn by MapLibre on vector tiles; Leaflet with raster tiles is the fallback* | Non-technical friends must be able to subscribe |
| D-20 | Map picker page shows rain as an image overlay with a **time slider** | Added during the interview; drives the overlay renderer (§11) |
| D-21 | Radar decoding: **own minimal decoder** in the runtime; `wradlib` is a **test-only** dependency used as the golden reference | See §5 — answers the "wradlib or alternatives" question |
| D-66 | **The service worker confirms a push signup itself; the notification says it is done.** On a confirmation push the worker POSTs `/confirm` exactly as the confirm page would - the token, proof that this browser holds the subscription, a fresh device key (`devicekey.js`, imported into the worker) - and only then shows *Erfolgreich angemeldet - ab jetzt bekommst du hier eine Benachrichtigung, wenn bei dir Regen aufzieht* (tap: settings). Open pages are told (`rainalert-confirmed`) and say the same in place of the waiting line. If the worker cannot confirm (offline, refused, 15 s passed) it shows the old confirmation and hands the link over (D-65) | Reported 2026-10-10: after D-65 the signup completed by itself, but the notification still asked to be tapped to activate - which by then was false. No new trust: the token was decrypted by this browser and opening it is what a tap does (D-36); the same redemption checks apply (proof before the token is spent, `Sec-Fetch-Site` same-origin). Accepted residual: a confirmation that succeeds only after the 15 s bound shows the fallback as well, and the handed-over link then finds its token spent. Verified in Chromium with a real worker and a push delivered through DevTools: page open, page closed, and a worker without the subscription (fallback) |
| D-65 | **A push confirmation completes without its notification being clicked.** The service worker keeps the confirmation link it receives (IndexedDB `rainalert-sw`/`pending`, this origin only, only `/confirm#a=` links) and posts it to open pages; the start page takes it once and goes to `/confirm`, on arrival or the next time it is opened. The confirmed page forgets the link and closes the notification. Tapping the notification still works | Reported 2026-10-10 on desktop Chrome on a Mac: clicking the confirmation did nothing, so signing up was impossible. `registration.getNotifications()` came back empty while macOS still showed the notification - Chrome had dropped it, and a click on it reaches no service worker. Nothing on the page can make that click arrive; the worker, which already holds the link, can. No new trust: the link is the same single-use token, it never leaves the browser that decrypted it, and opening it is exactly what a tap does (D-36). Verified in Chromium with a real worker and a push delivered through DevTools, page open and page reopened later |
| D-64 | **A push subscriber's settings open with a device key, not a session; notifications carry no buttons.** The browser registers a non-extractable WebCrypto ECDSA P-256 key together with a push-delivered single-use token (the confirmation or a settings link) and signs every settings request with it (`Authorization: RainKey`, over origin, method, raw path, server time and body hash). No cookie, no CSRF value, nothing to expire; the key rotates silently once a day and dies with the subscription. Only subscribe and unsubscribe are offered - no sign-out, no word about keys. Full design and its three security reviews: docs/PLAN_DEVICE_KEY.md | The push round trip proved the right thing (only the subscribed browser can decrypt the push) but cost availability and friction: muted notifications or a push-service hiccup locked people out of their settings, including deletion, and every visit cost a push. A key bound to that same proof is as strong, is cheap to check per request, and makes a session pointless (PLAN §9), which also removes CSRF for key holders. A long-lived cookie would have done nearly as well; the key wins only against copy-the-bytes leaks (HAR files, cookie exporters). Push token redemptions now require the browser holding that subscription (endpoint or `p256dh`), checked before the token is spent, and refuse cross-site POSTs by `Sec-Fetch-Site` - which closes login CSRF too. Pages from before the release redeem as before. The notification buttons, the durable request token behind them and `/manage/request` are gone; the liveness job's "acted on nothing" signal is now `last_seen_at` (a tapped warning, an opened settings page). Push subscribers no longer get the long-lived API token. Kill switch: `DEVICE_KEY_LOGIN_ENABLED` |
| D-63 | **One global daily cap on confirmation and settings-link mails** (`transactional_mail_cap_per_day`, default 50). Counted across all requests, in one bucket, never per IP | The per-IP limits can be sidestepped in production: Cloud Run's run.app address answers directly, and with `trusted_proxy_hops = 2` a request sent there chooses its own client IP (SECURITY_REVIEW.md F-5, status 2026-10-09). The per-address limits still cap what one mailbox receives, but not how many different mailboxes one attacker can have us write to, and those mails spend the provider quota and the domain reputation the warnings need. Only real sends count, so unknown addresses do not use up the day. Past the cap, nothing is sent, the answer is unchanged, and an error is logged. Push messages are free and not counted; warnings keep their own cap (`global_alert_cap_per_day`). 50 is far above a friends-and-family day; the cost of hitting it is a new subscriber whose confirmation does not arrive until tomorrow |
| D-62 | **The vector map zooms to 20 (Leaflet scale), and house numbers are readable.** `radar-gl.js` `MAX_ZOOM = 20`, two levels past the Leaflet fallback's 18. House numbers are solid grey with a halo in the building colour, and sized like the street names (10 to 13 px) | Asked for after using the vector map: it stopped zooming one level after the house numbers appeared (they start at Leaflet 18), and the numbers were hard to read. Vector tiles stay sharp when scaled, so the limit was the raster map's, carried over; the z14 data has nothing finer to show beyond 20. The numbers as generated were the label colour at 30 % opacity, about 1.9:1 against the building fill; now 5.7:1 (light) and 4.9:1 (dark), above WCAG's 4.5:1. At 8 to 10 px they were also small print on a phone. `build.mjs` stops if the building fill changes upstream, so the contrast is rechecked rather than lost. The radar still goes blocky long before 20, which is honest about 1 km cells |
| D-61 | **The OSMF vector tile usage policy was read (2026-10-08) and the service meets it.** It permits external use - "in principle happy for our map tiles to be used ... for creative and unexpected uses", with no exclusion of applications, unlike the raster policy that ruled OSM tiles out in Q-5 - on minimum requirements, best effort, no SLA, and access may be blocked without notice | Read from the policy text (the page itself was not reachable from the development sandbox). **Required, met:** licence attribution, bottom right (MapLibre's attribution control, linked to /copyright); a valid User-Agent and, from a web page, a valid Referer (the browser's own UA; the site's origin as Referer, D-58); no no-cache headers and tiles cached per their expiry (browsers do both - checked in Chromium with the HTTP cache on: no `Cache-Control`/`Pragma` on tile requests, and a second visit fetched nothing; a run with Playwright's request interception showed `no-cache` on every tile, but that is interception switching the cache off, not the page); no bulk downloading, no caching proxy. **Recommended, now done:** the tile URL is not hard-coded - `vector_tile_url` is a Terraform variable, so switching provider or turning the vector map off is an apply, not a rebuild; a "Karte verbessern" link to /fixthemap next to the attribution; a contact address in every page's footer through `contact_email` - optional and empty by default, because it is published and the operator chooses it. The privacy page links the OSMF privacy policy when its server is in use. **Versioning:** a new Shortbread major version gets a new URL path; the old tiles stay updated for a month and available for two more - RUNBOOK §3c says what to do |
| D-60 | **The radar is drawn as a gradient between the band colours, not seven flat steps.** Each band's colour is drawn exactly at the value where the band starts; a value inside a band is shaded toward the next band's colour by how far along it is, on a log scale, in 32 shades per band. The legend, the threshold picker and alerting are unchanged | Flat steps hid most of what the radar reports: since D-52 `Nieselregen` runs from 0.01 to 0.15 mm/5 min - fourteen distinct readings, all one pale blue - so a drizzle field and the edge of a shower looked the same. **Anchored at the band starts** so the seven colours the legend and the picker show still name what is on the map. **Log scale** because that is how the band starts are spaced (each roughly double the last) and how rain is felt; linearly, the lower half of every band would barely change colour. Interpolated in sRGB with alpha: the stops are hue neighbours, so the straight line between two passes through no muddy middle. Above the last band start (6 mm/5 min) the colour stays the last one. **Shades, not a continuous blend:** continuous made a wet frame 282 KB instead of 77 KB. 32 shades per band is 225 colours, which fits a PNG palette (one byte a pixel, alpha per entry in tRNS) - 107 KB, and the render got faster - and is the smallest count at which every one of `Nieselregen`'s fourteen readings gets its own shade (16 gave 11). Readings are snapped to 1e-6 before the scale is applied, because the decoder's float32 puts 0.35 just below 0.35 and a band's start must get exactly the band's colour - the same trap D-52 fixed in the sampler. Verified on a real frame on both map engines. Frames rendered before the deploy keep flat colours until they age out of the window |
| D-59 | **The vector map is the default on both map pages, and the dark style is lighter.** The `?karte=vektor` switch is gone: wherever `VECTOR_TILE_URL` is set (by default), the start page and the settings page draw with MapLibre; Leaflet is the fallback (no WebGL, no modules, MapLibre failed to load) and the whole engine when the setting is empty. The dark style is generated with `recolor: { gamma: 0.6, contrast: 1.1 }` | Tried on the real tiles and judged good to go, with one complaint: dark mode too dark and too flat. Measured, it was a near-black background (rgb 39, 2% luminance) with black water, land/water 1.4:1, and the translucent rain colours sinking into it. A gamma lift spreads the dark tones apart instead of raising them evenly: background 78, land/water 2.5:1, borders and roads clearer, labels still white on a dark halo; lighter candidates (background 97+) started to read as a grey slab on the dark page. **The settings page** needed one thing the start page did not: its script runs inline during parsing, so it must not choose an engine before the MapLibre module has run. It waits for the `DOMContentLoaded` *event* - an earlier version checked `document.readyState`, which turns 'interactive' when parsing ends but before modules run, and with MapLibre loading slowly it built a Leaflet map; caught in Chromium by delaying the library 2.5 s. Both engines' `createMap` now open on a place and zoom as well as on bounds |
| D-58 | *(The opt-in was replaced by D-59: the vector map is now the default on both map pages.)* **A vector map trial on the start page, opt-in by `/?karte=vektor`: MapLibre GL JS 6 on OpenStreetMap's Shortbread vector tiles, with the radar drawn under the place names.** The ordinary start page and the settings page stay on Leaflet | The case for it is specific to a rain radar: on raster tiles the place names are baked into the image and the radar paints over them, so the town a shower is over is the label you cannot read. With vector tiles the radar goes between the landscape and the labels (`slot-below-labels`, a layer the VersaTiles styles leave for exactly that). It also gives a muted gray basemap that does not compete with the rain colours, a dark basemap in dark mode, and sharp rendering at the fractional start zoom of D-55. **What was built:** `radar-gl.js` offers the same functions as `RainRadar` (createMap, picker, mark, timeline, locateControl, legendControl) and signup.js picks one engine per load (`chooseEngine`); the radar loop, slider and bubble are not duplicated - `timeline` now draws through an overlay driver, Leaflet's or MapLibre's. Zoom numbers stay Leaflet's in signup.js and the adapter converts (MapLibre is one lower). **Kept off third parties:** MapLibre, its worker and the Noto Sans label fonts are vendored and hash-pinned; the styles are generated from `@versatiles/style` (MIT) by `scripts/map-style/build.mjs` and stripped of VersaTiles' glyph server and sprite (fonts come from the style's `font-faces`, new in MapLibre 6; the icon and hatch layers that needed the sprite are dropped - shop icons are noise on a rain map). The only new origin a browser contacts is the tile server, and it gets the site's origin as Referer, as the Leaflet tiles do. **CSP:** `connect-src` gains the tile server and the overlay bucket (MapLibre fetches both; the bucket's CORS already allows the site), `img-src` gains `blob:`; the worker is a same-origin module, so `worker-src 'self'` stands. **Costs:** ~430 KB compressed of script on the trial page against Leaflet's ~40 KB (cached a year after the first visit, D-57); WebGL required, with an automatic fall back to Leaflet where it is missing; the OSMF vector service is best effort with no SLA. **Verified** in Chromium against synthetic Shortbread tiles - this sandbox cannot reach vector.openstreetmap.org: labels drawn above a full-cover test overlay, picking by click, drag and the locate button, a warning link opening at Leaflet zoom 11, the slider bubble, dark mode, Leaflet fallback with WebGL disabled, no console or CSP errors - and one bug found that way and fixed: MapLibre puts markers inside the map's container, so tapping the pin counted as a map click above its tip and moved it ~30 km north per tap. **Not verified:** the real OSM tiles, and how the gray style looks with real data; the OSMF policy text could not be read from here either |
| D-57 | **Scripts and styles are referenced by content-versioned URL** (`/static/radar.js?v=<12 hex of sha256>`, through the `static_url()` template global) **and cached for a year; anything else under `/static` is `no-cache`** (`api/assets.py`) | D-56 shipped and only Opera showed the bubble: `/static` was served with an ETag and Last-Modified but no Cache-Control, which lets a browser reuse a file without asking for a tenth of its age - so Chrome, Edge and Firefox on the phone, and Chrome on a desktop, kept the previous `radar.js` after the deploy, and Opera, with nothing cached, got the new one. Every deploy that touched a script had this window; it was invisible until a change was big enough to look for. A new file is now a new URL, so no cache can stand in for it, and because the URL names the content it can be `immutable`. Only the *current* version gets that header: a page from before a deploy asks for the old `v`, receives the new file, and must not pin it to the old URL. The version is keyed on the file's mtime and size, not computed once, because locally files change under a running server. The vibration went from 12 to 30 ms in the same change, on the guess that 12 was too weak to feel; once the current script was running, 30 felt too strong, and it is back at 12 |
| D-56 | **While the time slider is held, a bubble over its knob says how far that frame is from "jetzt"; on Android a drag that crosses or lands on "jetzt" gives one 12 ms vibration (briefly 30 ms, which was too strong; see D-57).** Both in `radar.js`, so the start and settings pages get them. "Jetzt" is the latest radar image, as on the stamp line, and the bubble uses the stamp's own wording (`relative()`) | Above the knob because on a phone the finger covers the knob and the stamp line under it. Kept inside the row at both ends rather than centred and cut off; shown from the first touch, hidden on release, during playback, and on blur. `aria-hidden`, with the slider's `aria-valuetext` carrying the same words to a screen reader. The vibration fires only from the `input` event, which the reader's own moves raise and playback and range changes do not, so the animation never buzzes; it fires on arrival at 0, not again on leaving it, and also when a step jumps over 0. Limits, accepted: Safari has no vibration API and Firefox removed it, so iPhones and Firefox get none; Chrome grants vibration only after the page has had a tap or click, so the very first drag of a visit, before any tap, is silent; the phone's own "touch vibration" setting wins. A haptic notch at "jetzt" was offered and declined |
| D-55 | **The start view is Germany, fitted to the map's size; the map's height cap is Germany's shape.** `signup.js` fits the first view to 47.27-55.06 N, 5.87-15.04 E without zoom snapping, then restores snapping; `base.html` caps the map at 1.36x its width, which is that box's aspect in Web Mercator | After D-54 the map grew but the start view stayed at a fixed zoom 5, at which Germany is ~210x320px whatever the map's size: on a desktop a third of a 544x816 map, the rest empty - which read as "the map is too high". Whole zoom levels cannot fit it, because Germany is 211px wide at zoom 5 and 423px at zoom 6 and the map is 330-544px wide, so the first view alone is fractional (5.5-6.3) and the first press of + or - lands on a whole level again, where tiles are drawn at their own size. The cap was 1.5 from a miscalculation; measured in the browser Germany's box is 1.36, so a tenth of the map stayed empty below it. Now Germany fills the map: 354x481 of 378x515 on a 412px phone, 518x704 of 542x738 on a desktop. A test recomputes the aspect from the box in `signup.js` and fails if the CSS disagrees. Desktop map height: 816 -> 740px on a 1080px screen; on a screen 800px tall it stays 652px, limited by the slider having to stay visible |
| D-54 | **The radar map takes the screen's height, not a fixed share of it.** On `/` and `/manage` the map is `clamp(200px, 100svh - reserve, 1.5 x its width)`, where each page's reserve is what must stay on screen besides it: the headline above, the time slider and the timestamp below. The page's top padding is 1rem (was 2rem) on every page | It was 45svh / 46vh: on a 412x780 phone a 351px map with a third of the screen empty below the controls, and in landscape the slider was already off screen. Now 570px there. `svh` because it is the screen with the address bar shown, so the slider is visible before any scrolling, and unlike `dvh` it does not change while scrolling, which would redraw Leaflet under the finger. The reserve is in rem so it grows with the text, and was measured in Chromium (timestamp ~10px clear at 360x640) rather than added up. The cap is Germany's own aspect in Web Mercator - 1.36, corrected in D-55 from a miscalculated 1.5 - so more height would only add sea and Alps; it also keeps some page visible below the map on most phones, which is where a finger scrolls the page rather than panning the map. Accepted: below ~350px of height (a phone in landscape) the 200px floor wins and the slider takes a short scroll; a notice above the map (radar error, expired warning link) pushes the slider down by its own height; the column stays 34rem wide on desktop, by decision. Found while measuring: the threshold select on `/manage` was wider than a 360px screen, which made the browser zoom the whole page out - fixed with `min-width:0` |
| D-53 | *Withdrawn.* A keep-warm request from the ingest job to the web service, to avoid cold starts. Deployed, it made no difference to the ~8 s first load, and it was reverted (commit be4fdb8). The number is not reused, so the history stays unambiguous | The cause of the slow first load is still unmeasured; RUNBOOK "The first page load after a quiet spell is slow" says how to measure it |
| D-52 | **Nothing the radar reports is cut.** The first band, `Nieselregen`, starts at 0.01 mm/5 min (~0.12 mm/h), RV's own quantum, for the map and for the threshold picker alike - so the faintest rain is drawn and can be warned on. It was 0.05 (~0.6 mm/h) | Prompted by drizzle that the DWD app showed, that was falling, and that this map did not draw: on a typical frame about two thirds of the wet cells sat below 0.05. The operator chose to see everything and judge the result rather than pick a new cut. Costs, accepted: the lowest steps carry the most non-rain echoes (clutter, insects, bright band), so the map is tinted more often and a subscriber on `Nieselregen` gets more warnings that turn out dry - bounded by the per-subscription daily cap (F-15) and by each subscriber choosing their own band; overlay PNGs grow (62 kB to 77 kB on a wet frame). Found on the way and fixed with it: the sampler compared float32 readings against double thresholds, and float32 cannot hold most hundredths, so a reading of exactly a band's start (0.01, 0.35, 0.70) counted as below it. Readings are now snapped to the product's precision before the comparison (`sampler.on_the_grid`); at a 0.01 floor the bug would have meant the faintest step could never trigger a warning. Stored thresholds are not migrated: a subscriber on the old 0.05 keeps it, shown as a custom value, until they pick `Nieselregen` again |
| D-51 | **Cold starts are shortened, not prevented.** The web service keeps `min_instance_count = 0`; its startup probe checks `/healthz` every second from the start (was: after 3 s, then every 5 s), and `startup_cpu_boost` is on | The first visitor after ~15 quiet minutes waits for a new instance, and no request reaches it until the probe passes - so with 5 s spacing an app ready at 3.5 s sat idle until 8 s. Probing every second caps that dead time at one second, at no cost: probes are not billed and `/healthz` touches nothing. The CPU boost speeds up the part that is real work (Python importing the app on one vCPU); it is billed at the normal CPU rate, for startup plus ~10 s, which at a few dozen cold starts a day stays inside the free tier. Rejected: `min_instance_count = 1` removes cold starts but bills an idle instance all month, several euros, against a stack whose point is being cheap; pinging the service from the ingest job would keep it warm for nearly nothing but Cloud Run does not promise to keep idle instances, so it is held back until the cheaper fix is measured. The probe's budget stays near the old one (30 s, was 33 s), so a broken image still fails its deploy. Moving the database would not help: it is not the cold part, Cloud SQL never scales to zero |
| D-50 | **The cycle-age SLI is alerted by logging it, not by exporting it.** `rainalert_cycle_age_seconds` lives in the database and is served at `/metrics`, which Cloud Monitoring cannot reach | Scraping it (Managed Service for Prometheus) or writing it as a custom metric from a scheduled job would each add a billable resource to a stack whose point is being cheap. The ingest job already runs every five minutes, so `log_cycle_staleness` logs the number there and a log-based metric filters the words - no new schedule, no new resource, no cost. Checked after the run and in the *caller*, because the failure being caught is a run that succeeds (fetch, 304, clean exit) while the data ages, so a check next to one of `ingest_once`'s returns would miss it. Two conditions, because a cycle stamped in the future reads as the freshest data we ever had and would silence a threshold on age (F-7). The trade: the log text is now an interface, so a test asserts the phrases in `monitoring.tf` against the phrases logged - rewording either alone disables the alert with nothing failing |
| D-49 | **Every page a notification can open re-reads its fragment on `hashchange`.** `sw.js` reuses an open tab by navigating it, so when the target path matches that tab's path only the fragment changes — a same-document navigation, no script, token unread, nothing happens | Fixed on `/` for warning links, then found still broken on `/manage` (press "Link an diesen Browser senden", stay on the page, tap the notification) and on `/confirm`, where the cost is a signup silently purged. The fix belongs on the page, not the worker: the worker navigating same-path is deliberate, because opening a window instead left one tab per notification. Two subtleties: re-running a page's bootstrap has to be safe (`L.map()` on an initialised container throws), and the re-entry guard has to queue rather than discard, or a tap arriving during a slow redeem is dropped — the same bug, rarer. The target set is derived from the `click_url`s `mail.py` builds, so a new one fails the suite until its page can be re-entered |
| D-48 | **One page.** The radar loop and the signup form share `/`; `/map` is deleted, not redirected. A visitor who had already subscribed still landed on a page whose whole purpose was to offer them a subscription, and the radar — the thing worth coming back for — was a separate address they had to know existed | The page asks the browser whether it holds a push subscription made with our current key (the same question `/manage` asks, same `sameKey` bias) and shows one of two things below the map: the signup form, or one line pointing at Einstellungen. Deliberately *only* that line — no location map, no unsubscribe — because those are the settings page rebuilt in a second place, and two screens that can disagree about a subscription are worse than one trip. The check is browser-side because no server endpoint can answer it without a session, and adding one would let anyone test whether a given push endpoint is registered here. It therefore answers "this browser believes it is subscribed", not "the server has a confirmed row": an abandoned signup leaves a live subscription behind, so the line says where to go rather than asserting more than it knows. Fail-open in three places — catch, 1.5 s timeout, no registration — because the failure that matters is a visitor who cannot sign up |
| D-22 | The slider spans **−12 h … +2 h by default, −48 h … +2 h at most** — the ceiling is everything DWD retains (measured: 47 h 55 min, `DWD_RV_FORMAT.md` §3). Past frames are the **t+0 analysis frame of each past cycle**; future frames are leads 1…24 of the **latest** cycle | Still one DWD product (RV); the past is what the radar saw, not a re-forecast. Revised twice: to −48 h on 2026-09-18 because a shorter window discards history that is free to have, then split into a default and a ceiling on 2026-09-19 because 577 slider positions is a poor thing to land on. `/?hours=N` and the picker under the map move between them, and the choice is remembered per browser in `localStorage`; the server renders the resolved window into `data-window-hours` and the script takes the slider from that, so the two cannot disagree — the heading that once said 12 h while serving 48 is gone, because a server-rendered sentence would now be wrong for every reader whose stored preference differs from the default |
| D-23 | `evaluations` is a **rolling 48 h debug log** with a single TTL. The permanent per-subscriber record is `rain_events` + `notifications`, which are only written when something happens anyway | §8.1 — an indefinite row-per-subscriber-per-cycle series outgrows the entire national radar archive at ~13 500 subscribers, and ~99 % of it says "nothing happened" |
| D-24 | Production database is **Cloud SQL `db-f1-micro`, `europe-west3`**; dev and CI use Neon free or a local Postgres | §6.3 — Neon's free CU-hour allowance does not survive a 5-minute cadence, and it would add a second US processor for email + home coordinates |
| D-25 | The settings page is reached by a **magic link**: one-use, 15 minutes, redeemed for a signed session cookie lasting 30 minutes | Answers the question F-16 left open. The API token cannot be the way in - it is shown once and is normally lost - and a permanent link in every alert would be a bearer credential to someone's home coordinates living in an inbox. A link that expires and is spent on first use is neither |
| D-26 | Every link we send carries its token in the **URL fragment**, never the query string | A fragment is not sent to the server, so it cannot reach a request log, a proxy history or a `Referer` - which is exactly the leak F-4/F-8 describe for `?token=`. `/confirm` and `/unsubscribe` have now migrated to this shape, and the signup QR with them (D-31). There is no exception left: nothing we send puts a token in a query string, and `GET /confirm` and `GET /unsubscribe` do not read one, so the old shape is gone rather than deprecated (D-33) |
| D-27 | A cookie-authenticated **write** additionally requires a CSRF value that was rendered into the page and is echoed in a custom header; a bearer-authenticated write does not | A cookie is attached by the browser to any request, including one another site caused; a bearer token has to be attached by script that already read the page, which the same-origin policy denies cross-site. The two credentials need different protection, not the same (F-16) |
| D-28 | Rule bounds: threshold **0.01 … 40.0 mm/5 min**, lead **5 … 120 min in steps of 5**, radius **0 … 20 000 m** | Both threshold ends come from the data rather than taste: 0.01 is RV's own quantum (`PR E-02`, and the floor of `numeric(5,2)`), and above `plausibility_max_mm_5min` a cycle is rejected at ingest so a higher threshold could never fire. F-15 also proposed capping lead at 60; **not taken** - the full forecast is what the product carries, and see F-15's own note on what that leaves open |
| D-30 | Re-rendering reads **only the first tar member** (`read_analysis_frame`), not the whole archive | bz2 is a stream, so reaching member 25 means unpacking 1-24 on the way, and that unpacking is ~96% of the re-render's time. RV writes t+0 first, so one member is all that has to come out: measured 2.254 s and a 252 MB peak for a full 25-member cycle against 0.055 s and 9 MB. It deliberately cannot do the completeness and mixed-nominal-time checks `read_cycle` does - which is why it is a separate function, used only where the archive was already validated when it was stored, and why it raises rather than guessing if the first member is not t+0 |
| D-29 | Changing the rule does **not** reset the alert state; only a location change does (D-17) | The state describes a place, so moving invalidates it. A threshold describes what to do with what is already known - and resetting on every adjustment would let someone being rained on re-arm their own "rain is starting" warning by nudging a number |
| ~~D-31~~ | *Superseded by D-45: the QR is gone with the topic.*  The signup QR is **returned in the response body**, not fetched from a `/qr?text=` endpoint, which is removed | The topic is not a hint, it is the credential - whoever holds one can subscribe to it, ask for a settings link on it and read the location - so D-26's rule covers it: it must never travel in a URL that ends up in a log, and uvicorn and Cloud Run both log the query string. Inlining also retires the allow-list that existed only to stop the endpoint encoding somebody else's URL |
| D-47 | The signup page **says that clearing browser data ends the subscription**, before anyone signs up | There is no recovery from it and no way to soften it. The push subscription and the settings session live in the same site-data bucket, and Chrome clears them together - "Cookies und andere Websitedaten" unregisters the service worker, which deactivates the subscription per the Push API spec. So there is nothing left in that browser to identify anyone with: no address to mail a link to, no topic to type, nothing to read back. The three alternatives were each worse. A recovery email would have solved it and reintroduced the thing push exists to avoid, an address to collect. A recovery code is the ntfy topic again under a new name - an unmemorable string the reader must keep. Silence would have been a subscriber who re-signs up, gets a second row with default settings, and never learns their location and threshold were lost. One sentence at signup makes the failure legible instead, and re-subscribing takes under a minute |
| D-46 | A **notification to push subscribers who have heard nothing for 30 days**, whose real job is deleting the ones who have gone | A web push subscriber can leave without telling us: blocking notifications, clearing site data and uninstalling the browser all revoke the subscription, and none of them reaches this service. The only way we ever find out is that a send returns 404 or 410 - so with nothing to send, we go on holding somebody's home coordinates for a subscription that ended months ago. In a wet month the warnings discover it within days; through a dry spell, or for anyone whose threshold is never met, nothing ever does, and that last group is precisely who no other mechanism covers. Hence a floor on how long silence can last. It is skipped whenever an alert went out in the window, so it is not an extra message for anyone the warnings already reach. Email is excluded: a mailbox does not revoke itself, and an unsolicited monthly mail is closer to spam than to housekeeping. The job runs *weekly* although the threshold is 30 days, because a monthly run does not bound silence at 30 days: somebody quiet since just after one run is not yet 30 days quiet at the next, so the first run that can see them is the one after - about 60 days, against the 30 the privacy page states. Weekly makes the worst case ~37 days and costs nothing, since the threshold does the selecting |
| D-45 | **Web push replaces ntfy**, and the phone channel stops depending on a third-party app | The decisive argument was not privacy, which is where this started, but comprehensibility: the first thing a subscriber had to do was install an unrelated app, and nothing on the page could explain why a rain service needed one. On Android web push needs no install at all - Chrome delivers it to an ordinary tab - so the whole of that step disappears. What it costs, stated plainly: iOS still requires the site on the Home Screen, so the iPhone caveat moved rather than went (Q-11/Q-12 close as moot on the ntfy app and reopen as a Home-Screen prompt); desktop delivery only happens while the browser runs, which the browser route already suffered; notifications are transient, which cost the anchor message (D-40's neighbour) and forced the way out onto the settings page; `maxActions` is 2 where ntfy allowed 3; and the subscription dies with the browser's site data (D-47). Against that, ntfy.sh saw the plaintext of every warning and web push does not - the payload is encrypted to keys only that browser holds (RFC 8291), so the push service sees timing and size and nothing else. Self-hosting ntfy would have fixed the privacy half and kept the app; it also turns out to require VAPID keys and a co-hosted web app for background browser notifications, so it was not the smaller option either. The crypto is `py_vapid` plus `http_ece` rather than `pywebpush`, which would have brought `requests` and `aiohttp` and taken the HTTP call away from the injectable `httpx` transport every other adapter here is tested through; both are single-maintainer PyPI critical projects, which is a risk to watch rather than a reason to add a third dependency wrapping them. One thing this decision shipped and then un-shipped on the same day: a `/push/resubscribe` route for endpoint rotation, removed after review showed it converted a non-secret into a location leak - see the security table |
| D-44 | **Leaflet is served by this app**, from `static/vendor/leaflet`, byte-identical to the npm tarball and pinned by sha256 in the tests | It was on a CDN from 2026-09-17 (`caf7ad9`) because the machines doing the work could not reach unpkg to copy it, and the note in the security review said so. The thing that unblocked it was not new capability but checking a second source: `registry.npmjs.org` served the same bytes and, unlike a CDN, published a sha512 to verify them against, so the vendored copy is provably upstream's rather than merely plausible. That is also why `make vendor-leaflet` refuses to write anything on a checksum mismatch, and why the tests pin both files - a vendored dependency that drifts is worse than a CDN, because nobody can diff it against anything. The point of all of it is one line: `script-src` and `style-src` are `'self'` again, so no visitor announces themselves to a third party before the map draws. The source map is deliberately not vendored - its 78 sources carry no `sourcesContent`, so it would resolve to nothing; one devtools-only 404 is the cheaper price than 225 KB that cannot work or an edited `leaflet.js` that no longer matches upstream |
| D-43 | The basemap **defaults to basemap.de** (BKG, CC BY 4.0) rather than to nothing | §11.1 (*Basemap tiles*) has the reasoning; what belongs here is why the earlier no-default decision was right and still got reversed. The objection was to *borrowing* a volunteer-run service, not to having a basemap, and open government data published for reuse is not borrowed. The property that picked it over better-looking alternatives was the licence rather than the cartography: Stadia, Jawg and MapTiler restrict their free tiers to non-commercial use, so any of them would have had to be revisited the day this page carried an ad, whereas CC BY 4.0 has no such concept. Germany-only coverage is not the compromise it looks like - RV is a German product, so global tiles would render places the rest of the system cannot speak about. The cost is a WMTS `{z}/{y}/{x}` template whose reversal is silent, which is why it has its own test and `make verify-basemap` |
| D-42 | A **spent magic link falls through to the session this browser already has**, and says so; the complaint about it is reached only once that session has been ruled out too | Opening the link twice in one browser is ordinary - a second tab, a restored tab, a link tapped again - and the first open both spends the token and sets the cookie. So the second was answered "dieser Link gilt nicht mehr" while the reader was signed in, and reloading that same page then worked, which is what made it baffling rather than merely wrong. The cookie is the authority (D-25); a spent token subtracts nothing from it. `redeem` therefore reports and only the dispatch decides. It is not silent about it either: `#panel-note`, separate from the `#panel-banner` that `fill()` owns, explains why the page opened anyway - otherwise the second tab is indistinguishable from the first and the confusion just goes quiet. In a browser with no session a spent link still refuses, which is the case the single-use rule exists for |
| D-41 | `/manage` renders **four states, all hidden**, and the script reveals exactly one: `busy`, `sent`, `gate`, `panel` | The gate used to be the markup's default, so it was what you looked at while any other path was still working - and arriving from a notification (`#r=`) the "we sent you a link" answer appeared *underneath* a form asking for the topic it had just used. The deeper fault was that the default was load-bearing without being written down: `redeem`, `load` and the CSRF refetch each only `return`ed on failure and relied on the gate still being there, so what a reader ended up seeing depended on what had not happened yet. With nothing shown by default every exit has to name a state, which is what makes the page's behaviour reviewable. The `sent` state names the link's TTL and offers the form only afterwards, as "nichts angekommen?" - a subscription deleted elsewhere still holds a valid request token, so the link can be sent to a channel nobody is listening on. A 429 and a 500 also stopped giving the same answer, which had been sending people away for an hour over a fault on our side |
| D-40 | The confirm page's button is **hidden in the markup** and revealed only on the mailed path; the push path shows a spinner, and the button returns after 8 s if the submit has not landed | D-36 confirms a push link on open, so its button is a thing to press that is already being pressed - and it appeared for a moment on every push confirmation, long enough to reach for. Hiding it *from* script cannot fix that: the script runs after the markup is parsed, so by then it may already be painted. `hidden` plus base.html's `[hidden] { display: none !important }` is the only version that cannot flash. The fallback is kept but offered on evidence rather than up front - a dropped connection would otherwise leave the reader on a spinner forever. A `<noscript>` now says why the page needs script at all: the token is in the fragment, so without it there is nothing to read |
| D-39 | **One notifier per channel, chosen by the message** (`NOTIFIER=auto` → `RoutingNotifier`, email + webpush since D-45), and a deployment can turn the email channel off entirely (`EMAIL_CHANNEL_ENABLED=false`) | There was one notifier per process, applied to everything, which is fine while one channel is live and unsafe the moment both are: `NtfyNotifier` puts `message.to` in the URL path, so with `NOTIFIER=ntfy` an email subscriber's confirmation was published to `<server>/<their address>` - the address becomes a public topic name and the confirmation link becomes that topic's contents. `OutboundMessage.channel` carries the label and the router refuses a channel it has no transport for, because a fallback is the same bug wearing a helpful face. `console` and `file` stay single sinks on purpose, or a local run would start publishing to a public ntfy server. The flag exists because the first deployment has push working and no mail provider: accepting an address it cannot write to is worse than refusing it, since the page then says to check a mailbox nothing ever arrives in. It gates signing up, not delivery - an existing email subscriber keeps working and can still reach `/manage` - and Terraform derives it from `smtp_host` so the page, the API and the mounted secret cannot drift apart |
| D-38 | Tapping a warning opens the map **on the place that warning was about**, via a signed reference that stops resolving after `locate_link_ttl_minutes` (60) | The country view does not answer the question a warning raises, which is whether that shower is coming *here*. The coordinates are deliberately not in the link: a warning stays in a notification list for good, so a screenshot of one would be a home address in plain text - more than the message itself says, since it names a time and an intensity but never a place. `POST /api/v1/locate` resolves the reference instead, and answers an expired one exactly as it answers a forged one, because telling them apart would say whether the subscription behind an old link still exists. Push only: email ignores `click_url`, and a mail body is forwarded far more often than a notification. Once it expires the map opens where it always did, which is the point - last week's warning tells a reader nothing about where its owner lives |
| D-37 | One **navigation block on every page**, between the content and the footer: Start, Regenradar, Einstellungen, with the current page marked rather than dropped | Each page used to invent its own wayfinding - the front page was "Zur Anmeldung" from the map, "Zur Startseite" from the settings and "Neu anmelden" after unsubscribing - while `/confirm`, `/unsubscribe` and `/privacy` had none at all, so an expired link left the reader on a page with no way off it. Keeping the current entry in the list means the set never changes shape as you move around, which is what lets it be found by habit. Rendering goes through one `page()` helper so the site-wide context cannot be forgotten on a route added later, which is how those three dead ends happened. Datenschutz stays in the footer: it is not a place you go to do something. The radar is listed unconditionally - `has_map` means there is imagery, not that the page exists |
| D-36 | A **push** confirmation link confirms when it is opened (`#a=`); a **mailed** one still waits for a click (`#t=`) | The click is there for F-4, and every actor F-4 names is a mail scanner - SafeLinks, Proofpoint, Gmail's link handling. None of them sits between this service and a notification on a phone, so on push the click defends nothing and costs a step. What actually keeps a scanner from spending the token is not the click but the fragment (D-26): the URL a scanner fetches carries nothing the server sees, so it takes script *and* the fragment before anything happens, which is why the mail click is kept as the second layer for scanners that do run script. The marker is chosen in the message builder because nothing downstream can work it out - the token is opaque and the server never receives the fragment |
| ~~D-35~~ | *Superseded by D-45 (2026-09-27): there is no app to deep-link to and no topic to encode.* The desktop QR encoded the **`ntfy://` deep link**, not the topic's web URL, and the page asks which device should be warned rather than guessing | The code is scanned by the phone that wants the warnings, and ntfy's web page would subscribe *that phone* to web push - which its own docs say needs iOS 16.4 and the page on the home screen, and which is what a native app was chosen to avoid. Reverses the earlier reasoning that a camera would not open a custom scheme: it does, confirmed on Android (Q-11). The cost is that a scan does nothing at all when the app is missing, and a camera cannot say so - which is why installing is step 1, both stores are offered (the page cannot know what the phone is), and the step says in words that nothing will happen otherwise. The browser route is kept as a real alternative rather than a fallback, with its own cost stated where it is chosen: it only works while that computer is awake |
| D-34 | **Confirming opens the settings session itself**; the page that follows leads straight into `/manage` | Tapping the confirmation proves a token we sent to the channel came back, which is the same thing redeeming a magic link proves (D-25) - a few seconds earlier. Sending a second link to prove it again is ceremony. The session is the ordinary one: same length, same wall, same cookie, so nothing is bought by arriving this way rather than that one |
| D-33 | **No RFC 8058 one-click unsubscribe.** `List-Unsubscribe` carries the same fragment link a person clicks; `List-Unsubscribe-Post` is not sent | One-click would have the mail client POST the URI with a body it fixes itself and never run the page, so the token would have to sit in the query string where a log gets it (D-26) - and the handler reads its token with `Form(...)`, which does not see a query parameter, so every conforming request was answered 400 while the page's own form returned 200 and deleted. Both measured. It was therefore not a feature being traded away for privacy, it was a promise the server did not keep, and removing it costs nothing that worked. Without the POST header a mail client opens the URI instead of posting it, so one fragment link serves both. Re-adding one-click when email becomes a real channel is Q-13, and means writing the handler first |
| ~~D-32~~ | *Moot since D-45: there is no topic to rotate, and a push endpoint is issued by the browser rather than by us.* There was **no "new topic" button**. Recovering from a suspected topic leak is delete-and-resubscribe | Rotation was designed as far as a working two-phase shape (issue the new topic, confirm on it, retire the old one only then) which does genuinely evict a passive watcher. It was dropped anyway, because deleting and signing up again *already* evicts, at no code cost: the new topic is minted in a session the watcher is not in. Rotation's real marginal benefit is therefore keeping the location, threshold, lead and radius rather than retyping them - about thirty seconds, in a scenario that is already rare - against a new token purpose, an enum migration, a pending-topic state, a second confirm endpoint and every half-state to test. It would also add a destructive one-way control to a page an attacker who has the topic can reach, quieter than `DELETE`, which at least sends a deletion receipt |

---

## 3. Glossary

- **RV** — DWD RADVOR product: quantitative precipitation **nowcast**, 25 frames covering t+0 … t+120 min in 5-minute steps, published every 5 minutes. Successor to the retired `composit/fx`.
- **DE1200** — the national composite grid: **1200 rows × 1100 columns**, 1 km cells, WGS84 polar-stereographic projection, covering Germany plus border areas.
- **Cycle** — one RV publication, identified by its **nominal time** `T0` (UTC, always a multiple of 5 min).
- **Frame** — one of the 25 grids in a cycle; frame `k` is valid at `T0 + 5k` minutes, `k ∈ [0,24]`.
- **Lead time** — minutes into the future: `5k`.
- **Sample** — the aggregated value (max over the radius mask) of one frame for one subscription.

---

## 4. Data source: DWD Open Data

### 4.1 Product selection

The folders used in the user's earlier project no longer exist:

| Old | New | Notes |
|---|---|---|
| `/weather/radar/composit/rx/` (past reflectivity) | `/weather/radar/composite/wn/` | Reflectivity composite on DE1200 |
| `/weather/radar/composit/fx/` (`FX[0-9]+\.tar\.bz2`, nowcast reflectivity) | **`/weather/radar/composite/rv/`** | **This is what we use** |

`composite/` currently also contains `dmax`, `hg`, `hx`, `hymecng`, `pg`, `rs`, `vii`.

**Chosen product: `RV`.** Reasons:
1. It provides exactly the requested **5-minute forecast resolution**, 25 steps out to +120 min.
2. It is **quantitative precipitation** (mm per 5 min), not raw reflectivity — no dBZ→rain-rate
   conversion (Z-R relationship) needed, so the threshold in D-13 is directly meaningful.
3. It already includes the t+0 analysis frame, so "is it raining *now*?" (needed by D-2) comes from
   the same file. **We do not need a second product for the past.** The same t+0 frames, kept per
   cycle, also supply the map's 12 h history (§11.1). `RS` (past-hour totals) is not required for v1.

Base URL: `https://opendata.dwd.de/weather/radar/composite/rv/`
Files: `DE1200_RV<YYMMDDHHMM>.tar.bz2`, plus a rolling `DE1200_RV_LATEST.tar.bz2`.

> **VERIFIED 2026-09-16** against a real archive — see `docs/DWD_RV_FORMAT.md`, which is now the
> authoritative description of the format. 25 members, one per lead, each a raw RADOLAN binary of
> 195 header bytes + 1200×1100 little-endian `uint16`. Header carries dimensions, precision
> (`PR E-02` → 0.01 mm), interval, nominal time and the lead itself, so nothing is hard-coded and
> filename parsing is never required.

### 4.2 Licence and attribution (mandatory)

DWD open data is **CC BY 4.0** (since 2023; previously GeoNutzV). Commercial use permitted with
attribution. The service **must** display, on the map page, on the privacy page, and in the footer
of every alert email:

```
Datenbasis: Deutscher Wetterdienst (DWD), Radarprodukt RV, CC BY 4.0
```

Derived/processed values must be marked as modified ("eigene Verarbeitung von DWD-Daten"), and
this service modifies heavily - the RADOLAN grid is reprojected to Web Mercator, coarsened to
~2 km and colour-mapped. The credit therefore reads:

```
Datenbasis: Deutscher Wetterdienst (DWD), Radarprodukt RV, CC BY 4.0
- eigene Verarbeitung (umprojiziert, vergroebert, eingefaerbt)
```

It lives in `rainalert/attribution.py` and nowhere else. It was four copies until 2026-09-21 -
footer, messages, and both timeline payloads - and none of the four carried the modification
notice this paragraph has always asked for.

### 4.3 Politeness policy toward opendata.dwd.de (hard requirements)

DWD publishes no explicit numeric rate limit, but the server is a shared public good and is known to
throttle abusive clients. The ingest worker **must**:

1. Perform **exactly one** product GET per 5-minute cycle in the happy path. Never per subscriber,
   never per request.
2. Send a descriptive `User-Agent`:
   `RainAlert/<version> (+https://<your-domain>; contact: <ops mailbox you control>)`.
   Do **not** put a personal/private address here.
3. Use conditional requests (`If-None-Match` / `If-Modified-Since`) when polling the `_LATEST` file;
   a `304` is the cheap, expected answer while waiting for a late cycle.
4. Never recursively crawl the directory tree. If listing is ever needed, use DWD's
   `content.log` / `content.log.gz` mechanism, which exists precisely for this.
5. Bound the wait for a late cycle: at most **5 attempts per cycle**, spaced with exponential backoff
   plus jitter (≈ 20 s, 40 s, 80 s, 160 s), then give up and let the next cycle handle it.
6. Honour `429` / `503` / `Retry-After` with backoff; after 5 consecutive failed cycles, open a
   circuit breaker (stop fetching for 15 min) and emit an operational alert.
7. Enforce byte budget guards — **per hour** (`DWD_HOURLY_BYTE_BUDGET`, default 512 MiB) as well as
   per day (`DWD_DAILY_BYTE_BUDGET`, default 8 GiB). Both halt ingestion rather than hammer DWD.
   Being a good citizen is a real requirement, but note which way these fail: **halting means nobody
   gets warned**, and the trigger is partly controlled by the other side. So the hourly budget makes
   exhaustion cost an hour rather than a day; a single capped response (§4.3.1) can never consume a
   meaningful fraction of either; and exhaustion **pages the operator immediately**, marks
   `/readyz` degraded, shows a banner in the UI, and is recorded in `radar_cycles.notes` so the gap
   in the timeline has an explanation. A silent day-long halt is not acceptable.
8. Be idempotent per cycle: a retried Cloud Run job execution must not re-download an
   already-archived cycle (unique key on `radar_cycles.nominal_time`).

### 4.3.1 The upstream archive is untrusted input (hard requirements)

DWD is a national weather service, not an adversary — but the *bytes* are untrusted all the same. A
compromised mirror, a hijacked route with a mis-issued certificate, a malicious proxy, and a plainly
corrupt publication are indistinguishable at the decoder, and because `_LATEST` re-serves the same
bytes every cycle, one bad file is an **indefinite** outage rather than a single failure.

1. **Cap the download.** Reject a response whose `Content-Length` exceeds 32 MiB, and stream with a
   hard byte counter that aborts at the same limit — `Content-Length` is attacker-supplied too.
2. **Cap the archive before reading it.** At most 32 members; every declared member size under
   8 MiB; declared total under 128 MiB. Reject the archive *as a whole* — decoding "the good parts"
   of a suspicious file lets the sender choose which forecast steps we see. Implemented in
   `decoder._check_limits`; a 483-byte archive declaring 512 MiB is a real, tested case.
3. **Read bounded.** Never `handle.read()` without a length; never `extractall()`; nothing is ever
   written to disk, so tar path traversal does not apply and must not start applying.
4. **Validate header fields against ranges, not just presence.** Reading dimensions and precision
   from the header (§5) is right, but unvalidated it means trusting a remote party with our
   allocation size and the scale of every reading: a forged `PR E+20` otherwise yields
   `precision = 1e+20`. Implemented in `decoder.parse_header`.
5. **Validate the nominal time against our own clock.** Reject a cycle stamped more than 15 minutes
   in the future or 3 hours in the past, and page rather than store — see §15, where this value is
   the key SLI. Cross-check it against the response's `Last-Modified`, which tracks it to within
   about 5 minutes (`DWD_RV_FORMAT.md` §4); a larger disagreement is an integrity signal.
6. **Plausibility-gate the decoded field** before any evaluation, recording the verdict in
   `radar_cycles.status`. Reject and page on: national max above 40 mm/5 min, a no-data fraction
   outside the observed 45–55 % band, or an implausible jump in wet fraction against the previous
   cycle. A gated cycle freezes state — it must never be evaluated as "dry" (§9 step 0).

### 4.4 Publication timing

RV for nominal time `T0` appears a few minutes after `T0`. **Measured** over a full 48 h listing
(see `docs/DWD_RV_FORMAT.md` §4): typically **+3 m 10 s … +3 m 30 s**, occasionally **+4 … +5 m**,
worst observed **+5 m 13 s**.

Therefore: Cloud Scheduler fires at **minute 4, 9, 14, … 59** (`cron: 4-59/5 * * * *`, UTC), and the
job then applies the bounded retry loop from 4.3 §5. Minute 4 puts the *first* attempt after the
common case, so the usual cycle costs exactly one request; the backoff (20/40/80/160 s, cumulative
300 s) still covers the five-minute tail. Firing at minute 3 — as this document specified before the
delay was measured — would land before publication on most cycles and burn a retry every time.

Note that a job started at minute 4 for `T0` is still fetching data for `T0`, not `T0−5`: the nominal
time comes from the file header (`_LATEST` carries no timestamp in its name), and `radar_cycles`
dedupes on it, so a cycle that slips past the next firing is simply picked up by that one.

Data staleness is a first-class monitored metric (§15) — a stale cycle must never be silently treated
as "no rain".

---

## 5. Radar decoding: wradlib or not? (answers the open question)

**`wradlib` is the correct reference implementation and the wrong runtime dependency for this service.**

| | wradlib at runtime | Own decoder + wradlib as test oracle (**chosen**, D-21) |
|---|---|---|
| Correctness | Battle-tested, maintained, handles all RADOLAN variants | Only handles RV; validated against wradlib in CI |
| Deps | numpy, xarray, xradar, optional GDAL/netCDF stack; image ≫ 1 GB | numpy + pyproj only; image ≈ 200 MB |
| Cold start | Slow imports hurt a 288×/day Cloud Run job | Fast |
| Format drift | Upstream fixes it for you | You own it — mitigated by the golden test |

**Decision:** implement `rainalert/radar/decoder.py`, roughly 150–250 lines:

1. Read the ASCII header up to the `ETX` (`0x03`) terminator; parse it into a dict of fields
   (product id, nominal datetime, dimensions `GP`, precision `PR`, interval `INT`, forecast minutes
   `VV`, format version `VS`, and the secondary-data / flag definitions).
2. Read the payload as `numpy.uint16` little-endian, reshape to `(rows, cols)` taken **from the
   header**, not from constants.
**Decompress once (2026-09-19).** `read_frames` unpacks the bz2 itself, in bounded chunks, and
hands the plain tar to `tarfile`. Letting `tarfile` read the compressed stream directly cost the
decompression roughly twice: it scans for member headers and then seeks back to each member's
data, and a backwards seek in a bz2 stream restarts decompression from the beginning. Measured on
the three-frame fixture: 204 ms of CPU before, 122 ms after. Unpacking is ~80% of the cost of
reading a cycle, so this is the only part of the decoder worth tuning; the numpy conversion is the
other 20%.

The bound matters as much as the saving. `bz2.decompress` would materialise whatever the archive
claims before any limit could look at it, which is exactly the bomb of F-1, so the decompressor is
fed in chunks with a capped output per call, the running total is checked against
`MAX_TOTAL_BYTES`, and the first tar header is inspected as soon as its 512 bytes exist. The 483 B
→ 512 MiB bomb is refused after half a kilobyte, with a measured peak allocation of 0.52 MB.

3. Apply the **precision factor from the header's `PR` field** (e.g. `E-02` → ×0.01) to convert raw
   counts to millimetres per 5-minute interval. Do not hard-code the exponent.
4. Identify no-data by **comparing against the sentinel `0x29C4`**, before any bit masking. Missing is
   **not** zero and **must not** be interpreted as "dry" (§9, step 0).
   **Never strip flag bits blind:** `0x29C4 & 0x0FFF = 2500`, which after the precision factor is a
   plausible-looking 25.00 mm/5 min — so the naive decode silently turns 47 % of the grid into
   extreme rain and alerts everyone, forever. Required test case (§16.1).
   Equally: **do not read data quality out of the header's `MS` radar-site list.** It lags reality — a
   site absent from `MS` can still be contributing, and vice versa (`DWD_RV_FORMAT.md` §5). Coverage
   comes from the no-data mask and nothing else.
5. Return `(values: float32 [rows, cols], missing: bool [rows, cols], header: dict)`.

Georeferencing (`rainalert/radar/grid.py`):
- Build the DE1200 → WGS84 transform once with `pyproj` from the projection parameters, and the
  inverse `lat/lon → (row, col)` mapping.
- **Pin it with a test** that compares against `wradlib.georef.get_radolan_grid(1200, 1100, wgs84=True)`
  at a set of reference points (corners, centre, a handful of German cities) with a tolerance of one
  half cell. This is the single most likely place to introduce a silent, systematic bug — an offset
  of a few cells means warning the wrong village.
- Cache the derived `(row, col)` and the radius mask per subscription; invalidate on location update.

`wradlib` lives in the `dev`/`test` dependency group only and is never imported by runtime code.
CI enforces this with an import-linter rule.

---

## 6. Architecture

```
Cloud Scheduler (cron 4-59/5 * * * *, UTC)
        │ OIDC
        ▼
Cloud Run Job: ingest ────────────────────────────────────────────┐
  1. conditional GET DE1200_RV_LATEST.tar.bz2  (≤5 tries, backoff) │
  2. dedupe by nominal_time (unique) ─────────────► Postgres       │
  3. archive raw .tar.bz2 ───────────────────────► GCS  (48 h TTL) │
  4. decode 25 frames (numpy, in memory)                           │
  5. sample per distinct radius mask (deduped) ► fan out to subs    │
  6. evaluate state machine ► enqueue alerts ─────► Postgres       │
  7. render overlays: 1 observed + 24 forecast ──► GCS (14 h / 1 h)│
  8. deliver queued alerts via Notifier ─────────► mail provider   │
└──────────────────────────────────────────────────────────────────┘

Cloud Run Service: api  (scale to zero, min-instances 0)
  FastAPI + Jinja2:  /  /confirm  /manage  /unsubscribe  /privacy
                     /api/v1/... , /healthz, /readyz, /metrics
        │                    │
        ▼                    ▼
   Postgres            GCS (overlay PNGs, public-read or proxied)
```

**Why steps 5–8 live in the same job as 4:** the decoded grids are needed by both the sampler and the
renderer. Measured, a complete 25-frame cycle as `float32` plus boolean masks is **165 MB** (the
66 MB figure an earlier revision used is the raw `uint16` size), before the renderer's reprojection
buffers — size the task from 165 MB, not from 66. Splitting would mean re-reading from GCS for no benefit at this
scale. If subscriber count ever makes step 6 slow, split 7 (rendering) into its own job first — it is
the only part that is not on the alerting critical path.

**Delivery ordering:** alerts are written to `notifications` with `status='queued'` inside the same
transaction as the state transition, then delivered. A crash after commit but before send is
recovered by the next cycle, which re-attempts `queued` rows older than 60 s and marks anything
older than 30 min `expired` (a late rain warning is worse than none).

### 6.1 GCP resource list

| Resource | Purpose | Notes |
|---|---|---|
| Cloud Run **job** `rainalert-ingest` | the 5-minute pipeline | `--max-retries 1`, `--task-timeout 240s`, **concurrency 1** |
| Cloud Run **service** `rainalert-api` | API + web UI | min instances 0, **`--max-instances` set explicitly** (see below) |
| Cloud Scheduler `rainalert-tick` | `4-59/5 * * * *` UTC | invokes the job via OIDC SA; minute 4 is measured, not guessed (§4.4) |
| GCS bucket `rainalert-data` | `raw/` (48 h), `overlays/obs/` (14 h), `overlays/fc/` (1 h) | per-prefix lifecycle rules, uniform ACL |
| Secret Manager | DB URL, mail API key, `SECRET_KEY` | mounted as env |
| Artifact Registry | one container image, two entrypoints | |
| Cloud Logging / Monitoring | structured logs, staleness alert | §15 |
| Postgres | **Cloud SQL `db-f1-micro`, `europe-west3`** | see §6.3 for why, not Neon free |

Cloud Run job concurrency must be 1 (plus a Postgres advisory lock `pg_try_advisory_lock` around the
pipeline) so a retried execution can never double-send.

`--max-instances` on the API service is a **security control, not a tuning knob**. Left at the
platform default, unauthenticated traffic to any endpoint that touches the database — `/readyz` does
so by definition — scales out until the database's connection ceiling is exhausted, which **starves
the ingest job of the connection it needs to alert anyone**, and bills us for the privilege.
Scale-to-zero means traffic costs money, so cost amplification is a real attack here rather than a
theoretical one. Set it low (single digits at this scale), give the ingest job its own database role
with reserved connections, and never let the web tier be able to take alerting down.

### 6.2 Cost estimate (rough, EUR/month, single-digit subscriber count)

| Item | Calculation | ≈ |
|---|---|---|
| Ingest compute | 288 runs/day × ~30 s × 1 vCPU / 2 GiB ≈ 72 vCPU-h/month, partly free-tier | 1–3 |
| API service | scale-to-zero, a few requests/day | ~0 |
| GCS raw archives | ~5 MB/cycle × 288/day ≈ 1.4 GB/day, 48 h retention ≈ 3 GB | <0.1 |
| GCS overlays | observed: 144 × ~150 KB rolling ≈ 22 MB; forecast: 24/cycle × 1 h ≈ 43 MB | <0.1 |
| Egress | overlay PNGs to a handful of browsers | ~0 |
| Mail | free tier (~300/day) | 0 |
| Database | Cloud SQL `db-f1-micro` + storage + backups | ~9–12 |
| **Total** | | **~11–17** — the database is most of the bill (§6.3) |

Numbers are estimates; the archive size depends strongly on how much it is raining (bz2 of a dry
grid is tiny). The daily byte budget guard (4.3 §7) is the backstop.

---

### 6.3 Why Cloud SQL and not Neon's free tier

Both are managed Postgres and the code is DSN-agnostic, so this is not a technical lock-in. Two
things decided it, and both are specific to *this* workload rather than general advice.

**The 5-minute cadence defeats scale-to-zero billing.** Neon's free plan allows 100 CU-hours per
month and suspends compute after an idle period, 300 s by default. The ingest job queries every
300 s, so the idle timer keeps resetting and the compute plausibly never suspends. At the 0.25 CU
floor that is roughly 730 h × 0.25 = **180 CU-hours against a 100 CU-hour allowance — exhausted
around day 16**, after which compute stays suspended until the month rolls over. That is a silent
two-week outage every month, in a service whose entire failure mode is "nobody gets warned and the
dashboard looks fine".

It can be tuned to fit — dropping autosuspend to ~60 s costs about 65 s of wakefulness per cycle,
roughly 39 CU-hours/month — but that is production running on a free tier trimmed to fit, with
arithmetic to re-verify whenever the cadence or the plan changes. Not worth €10/month.

**It would add a second processor for the most sensitive table.** We store email addresses and
precise home coordinates of private individuals in Germany. Cloud SQL in `europe-west3` keeps that
inside Google, which is already the processor for Cloud Run and GCS: one DPA, one subprocessor
entry. Neon is a second processor, US-headquartered since the Databricks acquisition, so the CLOUD
Act question exists even with data in an EU region — and §13 is built on data minimisation.

Cutting the other way: `db-f1-micro` defaults to about 25 connections, which makes the connection
starvation in SECURITY_REVIEW.md F-6 *sharper* than it would be behind Neon's pooler. That is an
argument for setting `--max-instances` low and giving the ingest job its own role with reserved
connections — both of which F-6 already requires — not for changing database. Note also that
shared-core instances carry no SLA; at this scale that is acceptable, and it is the reason to keep
the DSN-agnostic code rather than adopt Cloud SQL-specific features.

**Where Neon is the better answer:** outside GCP, for genuinely bursty workloads that are idle most
of the time, or when branch-per-PR databases are worth having. Hence the split actually adopted —
**Neon free (or a local Postgres, which is what the test suite uses) for development and CI; Cloud
SQL for production.** That costs nothing extra and puts Neon's branching where it is useful.

---

## 7. Data model (PostgreSQL)

```sql
CREATE TYPE subscription_status AS ENUM ('pending','active','paused','deleted');
CREATE TYPE alert_state       AS ENUM ('UNKNOWN','DRY','WARNED','RAINING');
CREATE TYPE cycle_status      AS ENUM ('ok','partial','failed');
CREATE TYPE token_purpose     AS ENUM ('confirm','manage','api','unsubscribe');

CREATE TABLE subscribers (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         text NOT NULL,                -- stored lowercased; CITEXT if available
  email_hash    bytea NOT NULL,               -- sha256(lower(email)) for lookups/rate limiting
  locale        text NOT NULL DEFAULT 'de',
  created_at    timestamptz NOT NULL DEFAULT now(),
  confirmed_at  timestamptz,
  deleted_at    timestamptz,
  UNIQUE (email_hash)
);

-- v1: exactly one row per subscriber (enforced by a partial unique index).
-- The separate table keeps 1:N possible later without a migration of the alerting code.
CREATE TABLE subscriptions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subscriber_id          uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  status                 subscription_status NOT NULL DEFAULT 'pending',
  lat                    double precision NOT NULL,   -- rounded to 4 dp at the API edge
  lon                    double precision NOT NULL,
  location_updated_at    timestamptz NOT NULL DEFAULT now(),
  grid_row               integer,             -- cached projection of (lat,lon)
  grid_col               integer,
  -- alert rule (D-14: per-subscription, defaults from D-13/D-3)
  radius_m               integer NOT NULL DEFAULT 2000  CHECK (radius_m BETWEEN 0 AND 20000),
  threshold_mm_5min      numeric(5,2) NOT NULL DEFAULT 0.15 CHECK (threshold_mm_5min > 0),
  lead_time_minutes      integer NOT NULL DEFAULT 30 CHECK (lead_time_minutes BETWEEN 5 AND 120),
  -- throttling (D-9/D-10: disabled by default)
  min_gap_minutes        integer NOT NULL DEFAULT 0 CHECK (min_gap_minutes >= 0),
  quiet_hours_start      time,                -- NULL = disabled
  quiet_hours_end        time,
  timezone               text NOT NULL DEFAULT 'Europe/Berlin',
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_subscription_per_subscriber
  ON subscriptions (subscriber_id) WHERE status <> 'deleted';
CREATE INDEX active_subscriptions ON subscriptions (status) WHERE status = 'active';

CREATE TABLE auth_tokens (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subscriber_id uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  purpose       token_purpose NOT NULL,
  token_hash    bytea NOT NULL UNIQUE,        -- sha256 of a 32-byte random token; plaintext never stored
  expires_at    timestamptz,                  -- NULL for long-lived api tokens
  used_at       timestamptz,                  -- single-use for 'confirm'
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz
);

CREATE TABLE radar_cycles (
  id            bigserial PRIMARY KEY,
  nominal_time  timestamptz NOT NULL UNIQUE,  -- always a 5-minute boundary, UTC
  fetched_at    timestamptz NOT NULL DEFAULT now(),
  source_url    text NOT NULL,
  etag          text,
  sha256        bytea NOT NULL,
  bytes         integer NOT NULL,
  frame_count   integer NOT NULL,
  status        cycle_status NOT NULL DEFAULT 'ok',
  archive_uri   text,                         -- gs://.../raw/DE1200_RV<...>.tar.bz2
  obs_overlay_uri    text,                    -- gs://.../overlays/obs/<nominal_time>.png
  fc_overlay_prefix  text,                    -- gs://.../overlays/fc/<nominal_time>/
  notes         text
);

CREATE TABLE evaluations (
  id                     bigserial PRIMARY KEY,
  subscription_id        uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  cycle_id               bigint NOT NULL REFERENCES radar_cycles(id) ON DELETE CASCADE,
  evaluated_at           timestamptz NOT NULL DEFAULT now(),
  now_wet                boolean NOT NULL,
  first_hit_lead_minutes integer,             -- NULL = no hit inside the lead window
  max_rate_by_lead       real[] NOT NULL,     -- 25 slots, mm per 5 min, index = lead/5 (NOT
                                              -- the frame's position: see 8.2), NULL where absent
  missing_fraction       real[] NOT NULL,     -- same indexing as max_rate_by_lead
  state_before           alert_state NOT NULL,
  state_after            alert_state NOT NULL,
  decision               text NOT NULL,       -- alert | no_rain | suppressed_state |
                                              -- suppressed_gap | suppressed_quiet |
                                              -- suppressed_cap | suppressed_daily_cap |
                                              -- skipped_missing
  UNIQUE (subscription_id, cycle_id)
);
CREATE INDEX evaluations_by_sub_time ON evaluations (subscription_id, evaluated_at DESC);
-- D-23: the whole table is purged past EVALUATION_RETENTION_HOURS (48 h, matching the raw archives,
-- so any incident inside that window is fully reconstructable). Nothing here is permanent; the
-- durable per-subscriber record is rain_events + notifications below.

CREATE TABLE alert_states (
  subscription_id uuid PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
  state           alert_state NOT NULL DEFAULT 'UNKNOWN',
  state_since     timestamptz NOT NULL DEFAULT now(),
  dry_since       timestamptz,
  current_event_id bigint,
  last_alert_at   timestamptz
);

CREATE TABLE rain_events (
  id                 bigserial PRIMARY KEY,
  subscription_id    uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  predicted_start_at timestamptz NOT NULL,
  first_alert_at     timestamptz,
  observed_start_at  timestamptz,             -- when t+0 first exceeded the threshold
  ended_at           timestamptz,
  peak_mm_5min       real,
  verified           boolean                  -- observed within ±15 min of prediction
);

CREATE TABLE notifications (
  id             bigserial PRIMARY KEY,
  subscription_id uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
  event_id       bigint REFERENCES rain_events(id) ON DELETE SET NULL,
  channel        text NOT NULL DEFAULT 'email',
  status         text NOT NULL DEFAULT 'queued',   -- queued|sent|failed|expired
  queued_at      timestamptz NOT NULL DEFAULT now(),
  sent_at        timestamptz,
  provider_message_id text,
  error          text,
  payload        jsonb NOT NULL
);
CREATE INDEX notifications_pending ON notifications (status, queued_at) WHERE status = 'queued';
```

Conventions: all timestamps `timestamptz`, stored UTC; presentation converts to
`subscriptions.timezone`. Migrations with Alembic.

---

## 8. Sampling

For each active subscription, per cycle:

1. `(row, col)` from the cached projection; recompute if `grid_row IS NULL`.
2. Radius mask: all cells whose centre is within `radius_m` of the subscriber point. At 1 km
   resolution and the default 2000 m this is ≈ 13 cells. Computed once and cached in memory, keyed by
   `(grid_row, grid_col, radius_m)` — many subscribers in the same town share a mask.
3. For every frame `k ∈ [0,24]`: `max_rate_by_lead[k] = max(values[mask] where not missing)`, and
   `missing_fraction[k] = mean(missing[mask])` — **per frame**. The no-data region advects with the
   forecast (measured: 102 615 cells go valid → no-data between t+0 and t+120, and 79 483 the other
   way; `DWD_RV_FORMAT.md` §9), so a point near the edge of coverage can have good data now and none
   at t+45. Taking frame 0's mask for all frames would evaluate garbage for exactly those users.
4. **The loop is fault-isolating.** Each subscription is evaluated inside its own try/except: a
   failure records `decision='error'`, increments a metric, and the loop continues. Without this, one
   unevaluatable row — a `NaN` latitude that arrived through the API, an unknown `timezone` string —
   aborts the run for *everyone*, every cycle, permanently, and §13's ban on logging exact
   coordinates makes it hard to find which row is at fault. A subscription that keeps failing is
   surfaced as unhealthy in `/subscriptions/me` and `/manage`, so the user is told rather than
   silently never warned.
5. Coverage is **not** the same as being inside the grid: the DE1200 rectangle is much larger than the
   radar network's reach, and ~47 % of it is no-data even in perfect conditions. A subscription is
   flagged `out_of_coverage` when its mask is entirely no-data at t+0 across several consecutive
   cycles — not merely when the point falls outside the grid. The UI/API says so explicitly rather
   than silently never alerting.

Rationale for `max` rather than `mean`: a 2 km radius around a point, one of whose cells is under a
shower, means the user gets wet. Mean would dilute small convective cells, which is exactly the case
this service exists for.

### 8.0 Index by lead, never by position

`max_rate_by_lead` and `missing_fraction` are indexed by **`lead_minutes / 5`**, built from a
`dict[int, float]` keyed on the frame's own `lead_minutes`. They are *not* the decoded frame list
indexed by position.

The two agree only when all 25 members are present with leads exactly 0, 5, … 120. If a cycle
arrives with `_015` missing, positional indexing shifts every later entry down one slot: rain at +60
is emailed as rain at +55, and the wrong `predicted_start_at` is written to the permanent
`rain_events` record — which then corrupts the verification job that exists to tune the thresholds.
With a forged `VV` field the misalignment is chosen by the sender. Absent leads stay NULL, which the
§9 per-frame gate already treats as "excluded, not dry".

The ingest path asserts completeness — leads exactly `range(0, 125, 5)`, one `nominal_time` — and
marks anything else `status='partial'`. (§16.1 tells the *fixture* tests not to assert a member
count, because fixtures hold three frames; that instruction must not leak into the pipeline.)

### 8.1 Why sample per subscription at all?

A fair objection: the radar field is one global object of fixed size, while per-subscriber data grows
with the number of subscribers. Past some N, storing anything per subscriber per cycle costs more
than storing the whole national grid — which contains strictly more information anyway.

That is true, and the crossover is low. One `evaluations` row is ≈ 300 bytes with its indexes; one
cycle's compressed RV archive is ≈ 5 MB. They cost the same at ≈ **13 500 subscribers** — and the
archive is a fixed cost per cycle at *any* N, while the rows multiply.

The flaw was not that the data is per subscriber. It is that it is **cycle-shaped**: 288 rows per
subscriber per day, ~99 % of which record "dry, nothing happened". Hence D-23 — `evaluations` is a
rolling 48 h debug log with one TTL and no exceptions, and the durable per-subscriber record is
`rain_events` + `notifications`, which are only written when something actually happens and so need no
retention machinery of their own.

Accepted limitation: once an event ages past 48 h, we know *that* we warned and when, but not the
exact rule values in force at the time. Recording those would mean another column and another code
path to answer a question a handful of users will ask about twice a year. Not worth it (see the
guiding principle in §1).

The sampling itself is deduplicated by mask key `(grid_row, grid_col, radius_m)`, so its cost is
bounded by the number of *distinct* masks rather than the subscriber count. That is a one-line
dictionary lookup, not an optimization project, and it is where the story should stop until a
measurement says otherwise.

---

## 9. Alert evaluation and state machine

Per subscription, per cycle, with `threshold = threshold_mm_5min`, `L = lead_time_minutes`:

```
now_wet   := max_rate_by_lead[0] >= threshold
hits      := { k : 1 <= k <= L/5 and max_rate_by_lead[k] >= threshold }
first_hit := min(hits) or None
```

**Step 0 — data quality gate.** If `missing_fraction[0] > 0.30`, record `decision='skipped_missing'`,
leave the state unchanged, and do nothing else. Radar outage must never be read as "dry" and must
never clear a `WARNED` state.

Frames beyond t+0 are gated individually: a frame whose `missing_fraction[k]` exceeds the limit is
excluded from `hits` rather than counted as dry, so losing coverage at long lead delays a warning
instead of suppressing one.

**States and transitions**

| From | Condition | To | Mail? |
|---|---|---|---|
| `UNKNOWN` | `now_wet` | `RAINING` | no — rain is already falling and we cannot tell for how long, so there is nothing useful to say |
| `UNKNOWN` | not `now_wet` and `first_hit` exists | `WARNED` | **yes** — dry here, rain approaching: we already know enough |
| `UNKNOWN` | not `now_wet` | `DRY` | no |
| `DRY` | `first_hit` exists and not `now_wet` | `WARNED` | **yes** (unless suppressed) |
| `DRY` | `now_wet` (rain started without a prior warning, e.g. formed overhead) | `RAINING` | no |
| `DRY` | otherwise | `DRY` | no |
| `WARNED` | `now_wet` | `RAINING` | no — the warned-about rain arrived |
| `WARNED` | no hit for 3 consecutive cycles (15 min) | `DRY` | no — forecast retracted |
| `WARNED` | otherwise | `WARNED` | no |
| `RAINING` | dry at t+0 continuously for `dry_clear_minutes` (default 30) | `DRY` | no |
| any | location moved > 1 km (D-17) | `UNKNOWN` | no |

On the `DRY → WARNED` transition: open a `rain_events` row with
`predicted_start_at = T0 + 5·first_hit`, queue the notification, set `last_alert_at`.

**Blast-radius limit (global, per cycle).** Before any mail is queued, count the transitions this
cycle would produce. If more than `min(50 % of active subscriptions, BLAST_RADIUS_MAX)` subscriptions
would be warned at once, queue **nothing**, record the cycle as `partial`, and send a single operator
alert instead. A national squall line is real and will trip this; at this scale a human confirming it
once is cheap, and it is the only control that bounds the worst case — a poisoned or corrupt cycle
that reads as rain everywhere would otherwise mail the entire list in one go *and* burn the mail
provider's daily quota, so that the day's genuine alerts are never delivered. There is also a
per-run absolute ceiling on mails, with confirmation mail drawing from a separate reserve so it
cannot starve alert mail.

**Suppression checks** (evaluated in this order, before queuing). In all of them the state still
advances to `WARNED`: rolling the transition back would re-fire the identical event on the next
cycle and achieve nothing but a delay.

1. `min_gap_minutes > 0` and `now - last_alert_at < min_gap_minutes` → `suppressed_gap`.
2. quiet hours configured and local time inside the window → `suppressed_quiet` (dropped, not queued
   for later — a warning delivered at 06:00 about rain at 03:00 is noise).
3. `alert_cap_per_subscription_per_day` alerts already queued for this subscription in the rolling
   24 h → `suppressed_cap`.
4. `global_alert_cap_per_day` alerts already queued across *all* subscriptions in the rolling 24 h →
   `suppressed_daily_cap`, and logged at ERROR.

The first two are the subscriber's own preferences and **disabled by default** per D-10; they are
checked first so that when they apply, the reason recorded is the one the subscriber chose.

The last two are limits imposed on them and are **on** by default (12 and 300) — they are what makes
D-9's "maximum notifications" defensible rather than an amplification lever (SECURITY_REVIEW.md
F-15, F-2). The ordering matters for what lands in `evaluations`: a cap is only interesting to see
there when nothing the subscriber chose would have stopped the send anyway.

Their asymmetry is deliberate. The per-subscription cap is fair — the account being capped is the
one that caused it — so it is a quiet counter. The global ceiling is shared fate: reaching it means
somebody is not warned about weather that is happening, for a reason that is not theirs, so it is an
error in the log. If it fires the question is whether the traffic is real, not whether to raise the
number.

Counted from `notifications` with `event_id IS NOT NULL`, so the six-monthly liveness ping (which
writes `event_id = NULL`) does not count against a rain-warning cap — which would otherwise make
every cap one tighter than it claims, silently.

**Why this shape:** a fixed cooldown either spams during showers or misses the second front. Tying
suppression to the physical event (it must go dry again for 30 minutes) gives exactly one mail per
rain event, which is what "not too many notifications" means in practice.

**Verification loop (cheap, high value):** `rain_events` rows are kept indefinitely (D-23), so a daily
job can compare `rain_events.predicted_start_at` with the later-observed t+0 frames and populate
`verified`, giving hit rate and false-alarm ratio. It runs inside the 48 h window, while the archived
grids are still there. Build this in M4; it is the only honest way to
tune the defaults in D-13 later.

---

## 10. API

Base path `/api/v1`. JSON in/out. Auth: `Authorization: Bearer <token>` with an `api` token.
All endpoints return RFC 7807 problem details on error.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/subscriptions` | none (rate limited) | `{email, lat, lon, radius_m?, threshold_mm_5min?, lead_time_minutes?}` → `202`. Always responds identically whether or not the address is already known (no account enumeration). Sends the confirmation mail. |
| `GET` | `/confirm#t=…` | confirm token | Renders a button and changes nothing; the `POST` behind it activates the subscription and opens settings access (D-34): for push, it registers the device key the page sent (D-64) - or, without one, opens a settings session; for email, it issues the long-lived `api` token and opens a session. A current page's push confirmation must come from the browser holding that subscription (endpoint or `p256dh`), checked before the token is spent; a cross-site `POST` is refused. Single use, 24 h expiry. The page reads the token from the fragment and the handler takes no `token` parameter at all. |
| `GET` | `/subscriptions/me` | api, session or device key | Current location + rule + state + last evaluation. Every authenticated answer is `Cache-Control: private, no-store`. |
| `PUT` | `/subscriptions/me/location` | api | `{lat, lon}` → `204`. The endpoint the future mobile app calls. Applies D-17. Rate limited to 1 per 60 s. |
| `PATCH` | `/subscriptions/me` | api or session | Update `radius_m`, `threshold_mm_5min`, `lead_time_minutes`. Absent fields are left alone. Bounds in §11.2. `min_gap_minutes`, quiet hours and `timezone` are still columns only. |
| `POST` | `/manage/link` | none | Ask for a settings link on a confirmed channel. Always `202`, known address or not. Rate limited to 5/hour. |
| `POST` | `/locate` | locate token | Resolves the reference a warning's tap target carries → `{located, lat, lon, radius_m}`, so the map can open on the place that warning was about. Stops verifying after `LOCATE_LINK_TTL_MINUTES` (60); an expired reference is answered exactly like a forged one, `{"located": false}` (D-38) |
| `POST` | `/manage/session` | manage token | Spend the one-use link. With a device key from a current page (and the browser holding the push subscription): register it and answer `{"enrolled": key_id}`, no cookie (D-64). Otherwise set the session cookie and return the CSRF value. A push mismatch is `403 {"detail": {"error": "push_mismatch"}}` and leaves the link unspent; a cross-site `POST` is refused. |
| `POST` | `/device-key/rotate` | device key only | `{device_key}` → `{rotated: key_id}`: replace the key that signed this request with a fresh one from the same browser. The page does it at most once a day (PLAN_DEVICE_KEY.md §4.5). |
| `GET` | `/manage/csrf` | session | A fresh CSRF value for a session already held, so a page reload does not cost an email. |
| `POST` | `/manage/extend` | session + CSRF | Renews the session to its full length, never past the wall the first link set (D-25). `409` once that wall is reached, which is the point of having one |
| `POST` | `/subscriptions/me/pause` / `/resume` | api | Temporarily stop alerts without deleting data. |
| `DELETE` | `/subscriptions/me` | api | Hard-deletes subscriber, subscription, tokens, evaluations, notifications. Returns `204`. |
| `GET` | `/forecast?lat=&lon=&radius_m=` | api | The 25 sampled values for an arbitrary point + a human summary (`"rain starting in ~20 min, light"`). Powers the app and manual testing. |
| `GET` | `/overlays/timeline?past_hours=` | none | The full slider manifest, default `TIMELINE_DEFAULT_HOURS` (12), capped at `TIMELINE_PAST_HOURS` (48), floored at 1: `{now, latest_cycle, bounds:[[s,w],[n,e]], width, height, colorscale:[…], attribution, gaps:[…], frames:[{offset_minutes, valid_time, kind:"observed"｜"forecast", source_cycle, url}]}`. `offset_minutes` is negative for the past, ordered ascending. Cache-Control 60 s. |
| `GET` | `/#l=…` | none (the reference is resolved by `POST /locate`) | The start page, which is the radar. With a warning's reference in the fragment it opens pinned on the warned location at zoom 11; without one, or once it has expired, on the country view (D-38) |
| `GET` | `/unsubscribe#t=…` | unsubscribe token | Renders a button and changes nothing; the `POST` behind it deletes. The `List-Unsubscribe` header carries this same link (D-33). |
| `GET` | `/healthz`, `/readyz` | none | Liveness / readiness. **Readiness = database reachable *and* its schema at the migration this code expects**, because new code on an old schema connects fine and then 500s on the first request touching what the migration added. `503` names the revision it found, the one it wanted, and the command. It does **not** keep such a revision from taking traffic: the startup probe is `/healthz` (D-51), so a revision whose migration has not run serves, and 500s. Run the migrate job after every apply that carries a migration (RUNBOOK §4). |
| `GET` | `/metrics` | internal | Prometheus-format metrics (§15). |

Rate limits (per IP and per email hash): `POST /subscriptions` 5/hour, `PUT location` 60/hour,
`GET /forecast` 120/hour. Implemented in-process with a Postgres-backed counter; good enough at this
scale, replaceable later.

---

## 11. Web UI

Server-rendered Jinja2, no build step, no SPA. Pages:

- **`/` — subscribe.** Map (MapLibre on vector tiles since D-59; Leaflet on raster tiles as the fallback), "use my location" button (browser geolocation, §11.3),
  draggable marker, email field, consent checkbox with a one-line purpose statement, submit.
  **Plus the rain timeline overlay + slider (D-20, D-22).**
- **`/confirm`** — result page; shows the API token once with a copy button ("you will need this for
  the app later"), and the manage link.
- **`/manage` — settings.** Threshold, lead time, radius and location, the last pickable on a
  map centred on the stored point at zoom 11, with the radius drawn as a circle and the current
  radar frame underneath. Reached by a magic link, not by the API token (§11.2). Pause/resume
  and delete are still not built.
- **`/unsubscribe`** — confirmation of one-click unsubscribe.
- **`/privacy`** — §13. There is **no `/attribution` page** and none is needed: §4.2 asks for the
  credit on the map page, the privacy page and in every alert, and the footer is on every page
  while `ATTRIBUTION` is in every message. A page would be one more place for the same sentence
  to drift out of date. If a paid tile provider or a vendored library ever needs crediting too,
  that is when a page earns its place.

### 11.1 Rain timeline overlay (−12 h … +2 h)

Two kinds of frame, both produced from RV — no second DWD product:

- **Observed** (`offset_minutes <= 0`): the **t+0 analysis frame of each past cycle**, one per cycle,
  5-minute spacing — 144 frames over 12 h. This is what the radar actually measured.
- **Forecast** (`offset_minutes > 0`): leads 1…24 of the **latest** cycle only, +5 … +120 min.

The asymmetry matters and must be visible in the UI: past frames each come from their own cycle,
while all future frames come from one. A user must never mistake a forecast frame for an
observation, so the two are labelled differently and the boundary at *now* is marked on the slider.

**Renderer** (ingest step 7), once per cycle:

1. Reproject the frame from DE1200 polar-stereographic to **EPSG:3857** into a fixed axis-aligned
   bounding box covering Germany, nearest neighbour, using `pyproj` directly. **Row 0 of the grid is
   the southern edge**, so the array is flipped vertically for a north-up image. Compute the mapping in
   the straightforward way each run; if it ever shows up in `rainalert_pipeline_seconds`, cache it
   then.
   *Why a Mercator box:* Leaflet's `L.imageOverlay` (and MapLibre's image source, D-58, given the box's four corners) only places axis-aligned, unrotated images by
   lat/lng bounds. Overlaying the native grid directly would be visibly skewed.
2. Map values to RGBA with a documented colour scale (transparent below the light-rain threshold, then
   a perceptually ordered ramp). Encode as palettised PNG at half resolution (≈ 550×600) — ~100–200 KB.
3. Upload, with different lifetimes because the two kinds are consumed differently:
   - analysis → `gs://…/overlays/obs/<nominal_time>.png` — kept **14 h** (12 h window + margin)
   - forecasts → `gs://…/overlays/fc/<nominal_time>/rv_<lead_minutes:03d>.png` — kept **1 h**, since
     only the newest cycle's forecast is ever served

**Manifest.** `GET /api/v1/overlays/timeline?past_hours=12` is a query over `radar_cycles` for the
window, returning frames ordered by `offset_minutes` ascending.

**Gaps.** A cycle the ingester never obtained is a hole in the timeline. The manifest lists missing
intervals explicitly and the client renders a gap — no overlay, a "no data" label — rather than
holding the previous image, which would fake continuity across a radar outage.

**Client.**

- Slider spans `−past_hours·60 … +120` in 5-minute steps (168 positions by default), starts at `0`
  (now), with a visible *now* tick separating observed from forecast.
- **Do not preload everything.** 168 PNGs is ≈ 25 MB — fine on a desktop, hostile on a phone with
  mobile data. Load the frame under the cursor, keep a window of ±6 frames prefetched, prefetch ahead
  in the direction of travel, and LRU-evict beyond ~40 images.
- Play button sweeps −60 min → +120 min at ~8 fps and loops; dragging the slider cancels playback.
- Labels show the absolute **local** time, the relative offset (`−45 min` / `+35 min`), the source
  cycle's nominal time, and `observed` / `forecast`.
- If the latest cycle is older than 20 minutes, the page shows a clear "radar data is stale" banner
  instead of pretending.

**Backfill.** *Implemented 2026-09-18.* A fresh deployment has no history, so the timeline starts
empty and fills at one frame per 5 minutes. `rainalert backfill --hours 12` fetches the cycles the
timeline is missing **sequentially** — one request at a time, oldest first, with a **jittered
0.3–3 s pause** between each. The band started at 1–15 s and narrowed twice under
measurement: the point of the jitter is to be unlike a metronome and to stop two instances
walking the archive in step, not to be slow for its own sake, and a full 48 h fill at the
original band took over an hour for no benefit DWD would notice.

Backfill also uses **gentler retries than live ingest** — one retry at a 5 s base, not five at
20 s. There, a cycle missed is a cycle gone: the next one is five minutes away. Here it is
picked up by the next run any time in the following 48 h, so spending up to five minutes on a
single archive buys nothing and makes the run look hung. Each fetch is logged with its duration
for the same reason: a long run must not be indistinguishable from a stuck one. All other §4.3 rules
are honoured unchanged: same byte budget, same circuit breaker, same response-size cap. It prints
the plan — how many cycles, how long, roughly how many MB — and asks before it starts, because
nobody should learn the size of a burst by watching a log scroll.

Three things it deliberately does not do. It **never evaluates alerts**: backfilled cycles are
history, and warning about them would mail every subscriber about rain that stopped hours ago,
once per cycle — `fetch_missing` takes no notifier at all, which is the cheapest way to guarantee
that. It **never retries a 404**: a cycle past DWD's retention window is counted and skipped, and
asking four more times would not bring it back. And it **refuses a file whose header disagrees
with the name requested** — a check live ingest cannot make, since `_LATEST` carries no
expectation.

The age check in `validate_cycle` is widened to the window asked for and only that; the
future check, the mixed-stamp check and the plausibility band all still apply.

A separate re-render path rebuilds observed overlays from the 48 h raw archives without
touching DWD at all — prefer it whenever the archive still has the cycle.
*How far back the `rv/` directory actually keeps files is an **M0 VERIFY** item*; if DWD only retains
a few hours, backfill can fill only that much and the rest accrues over time.

**Since D-59 the basemap is OpenStreetMap vector tiles** (`VECTOR_TILE_URL`, the OSMF's own server, drawn by MapLibre). What follows is about the *raster* basemap, which is now the Leaflet fallback's. Note that it rejects OSM's *raster* tile servers on their usage policy; the OSMF's *vector* tile service has a policy of its own, which was read on 2026-10-08 and does permit this use (D-61).

**Basemap tiles: resolved 2026-09-18, and not the way this section assumed.** The text below used
to read "fine for a private map picker"; it was wrong. OpenStreetMap's tile servers are volunteer
funded and their usage policy excludes applications outright, not merely heavy ones - and they
enforce it. A single developer instance was blocked, which is how this was found.

So from 2026-09-18 (`792db77`, *stop borrowing OpenStreetMap's tiles*) there was no default
provider at all: `MAP_TILE_URL` was empty unless configured, and the map drew the radar over a
graticule with a dozen cities marked. Borrowing a donated
service by default would have been taking something that was not offered, and would have shifted
the moment of failure from a developer's screen to a user's.

**Resolved 2026-09-27 (Q-5): the default is basemap.de Web Raster, colour variant.** The objection
above was to *borrowing*, not to having a basemap, and basemap.de is not borrowed — it is the
German federal mapping agency's (BKG) basemap, published as open data under CC BY 4.0 precisely to
be reused. Four properties decided it over the alternatives:

- **No key and no account.** Nothing to provision, nothing to rotate, nothing to leak. Every keyed
  provider puts its key in the rendered HTML, where it is public by construction and has to be
  referer-restricted to stop strangers spending the quota.
- **No quota.** Not a large free tier — no metering at all. There is no traffic level at which the
  map switches off or starts costing money, which removes a whole class of thing to monitor.
- **No non-commercial clause.** This is the one that separates it from the styles that look
  nicest. Stadia, Jawg and MapTiler all restrict their free tiers to non-commercial use, which
  would stop being satisfied the day this page carried advertising; CARTO drops from 5M to 1M
  tiles a month on the same event. CC BY 4.0 has no such concept, so the decision does not have to
  be revisited if the service's funding ever changes.
- **Its coverage is the radar's coverage.** Germany only. That reads as a limitation and is not:
  RADVOR RV is a German product on the DE1200 grid, so a global basemap would only render places
  this service can say nothing about. Expanding past Germany means finding another radar source
  (OPERA, or per-country services) — a data problem, not a tile problem — so paying for global
  tiles today would be buying coverage the rest of the system cannot use.

The trade accepted in exchange: it is WMTS, so the template is `{z}/{y}/{x}` rather than the
`{z}/{x}/{y}` the rest of the world uses, and getting that backwards renders a *scrambled* map
with every tile a real tile in the wrong place and no error anywhere. That failure is silent
enough to be worth a test of its own
(`test_the_default_basemap_template_puts_y_before_x`) and a `make verify-basemap` target, because
the neighbouring failure — a wrong path serving blank tiles — looks exactly like "no rain
anywhere" on a map whose entire job is showing rain.

Setting both `map_tile_url` and `map_tile_attribution` to `""` restores the graticule state. It is
still a supported configuration rather than a historical one: it is the answer for working offline,
and for anyone who wants no third party in the request path at all.

`Content-Security-Policy: img-src` is derived from whatever `MAP_TILE_URL` is set to, so the
policy can never be broader than the provider in use, and narrows to nothing when no provider is
configured. Choosing a *different* provider remains a deployment decision with no code in it —
two lines in `terraform.tfvars` and an apply, no image rebuild, because the digest does not change.

---

### 11.1.2 Fidelity of the overlay

The overlay used to be a **sample** of the radar rather than a picture of it, in two ways at
once, and the two compounded.

It was rendered at 560 px, so one pixel covered 1.5-1.8 km against 1 km cells; and each pixel
took **one** source cell (`np.floor` on the inverse projection) rather than considering the
others. Measured on a real frame: 28.8% of source cells appeared in the image at all, 66% of the
wet cells were never drawn, and the heaviest cell in the country - 11.80 mm/5 min - was absent,
because it fell between sample points.

Two changes, and it is worth being clear about which one does the work:

1. **`WIDTH` 560 -> 1120.** At the southern edge, the binding case in Mercator, a pixel is now
   0.90 km against 1 km cells. The mapping becomes **exactly one source cell per pixel** -
   measured mean 1.00, maximum 1 - so nothing is merged. This is what makes the picture faithful.
2. **Max-pooling.** Each pixel shows the heaviest of the cells that land in it, via a forward
   scatter computed once in `build_projection` and reduced per frame. At 1120 this is a no-op,
   since groups are single cells. It is there so the guarantee does not silently depend on
   `WIDTH`: at the old 560 a pixel held 2.9 cells on average and 89% held more than one, and
   that is the case the test suite exercises.

Max rather than mean, matching `sampler.sample`: a pixel one of whose cells is under a shower is
a pixel where you get wet, and averaging dilutes exactly the small convective cells this service
exists to catch.

**The guarantee, asserted per cell in `tests/test_overlay.py`:** every source cell is drawn at
least as strongly as it really is. Never weaker, never absent.

**The cost**, honestly: 62 kB a frame against 26 kB, so a full 168-frame timeline is 10.5 MB
instead of 4.5 MB; 246 ms a frame against 96 ms, so ingest spends ~6.2 s per cycle rendering
instead of ~2.4 s.

The projection itself is the other cost, and it is paid once: 20.9 MB of index arrays, and a
~60 MB spike while they are built. Both were larger until the arrays were narrowed to the
dtypes their values need (`int16` for grid indices whose maximum is 1199, `int32` for flat
offsets) and the forward map was built a band of rows at a time - a dozen 1200x1100 `float64`
intermediates at once was ~135 MB of peak the allocator then held on to, which on a 1 GB
machine is the difference between a job that runs and a box that stops responding. Re-rendering
250 archives holds steady at 165 MB resident, measured, from the first to the last. Bandwidth was the original reason for 560 and it is a real cost - but halving
the linear resolution of the product the service exists to show is a strange way to pay it.

**What is still lost, and deliberately:**

- ~~**Values below the first band.**~~ Nothing any more: the first band starts at 0.01 mm/5 min,
  the smallest step RV reports (D-52). Until 2026-10-07 the floor was 0.05, which left 98,300
  cells of that frame (0 < v < 0.05) transparent - a palette decision (§11.1.1), not a sampling
  one.
- **Exact values.** The PNG carries 32 shades per band (D-60), not numbers. A pixel says "this
  band, this far towards the next", which the legend's colours anchor at each band start.
- **Cells outside `BOUNDS`.** The DE1200 rectangle reaches 45.69-56.22 N, the image 46-55.9 N.
  Checked: no wet cell fell outside on the test frame, and the service area (47-56 N, 5-16 E) is
  strictly inside the image, so no subscriber's location can be clipped.

None of this affects **whether anyone is warned**. Alerting reads `frame.values` at full
resolution through `sampler.sample` and has never looked at the overlay.

### 11.1.1 The intensity scale

Seven bands, defined once in `radar/overlay.py` as `INTENSITY_BANDS`, and read by three things:
the overlay renderer, the map legend, and the threshold picker on the settings page. The legend and
the picker show each band's colour; the renderer draws that colour exactly at the band's start and
shades toward the next band's colour across the band (D-60). That is the
point of putting them in one place - a colour on the map and a colour in the dropdown mean the
same rain by construction, rather than because two lists were edited together.

| mm / 5 min | ≈ mm / h | Name | Colour | Opacity |
|---|---|---|---|---|
| 0.01 | 0.12 | Nieselregen | pale blue | 0.55 |
| 0.15 | 1.8 | leichter Regen | blue | 0.68 |
| 0.35 | 4.2 | mäßiger Regen | green | 0.78 |
| 0.70 | 8.4 | kräftiger Regen | yellow | 0.85 |
| 1.50 | 18 | starker Regen | orange | 0.90 |
| 3.00 | 36 | Starkregen | red | 0.94 |
| 6.00 | 72 | extremer Starkregen | violet | 0.97 |

**One opacity, not two.** The alpha above is what you see. It used to be multiplied again by the
Leaflet layer's own opacity — 0.75 on the radar, 0.6 on `/manage` — which put the lightest band at
an effective 0.38 and 0.31: pale blue at a third strength over a basemap, close to invisible, and
a palette in which no number was the number on screen. `LAYER_OPACITY` is 1.0 and the templates
read it from the server, so there is one place that decides how strong rain looks.

Changing these values only affects **newly rendered** PNGs. `make rerender` rebuilds the whole
stored timeline from the archives already on disk without touching DWD — raw archives are kept
for the same window the map shows (`raw_retention_hours`), so it covers everything visible.

**On the green band.** `mäßiger Regen` is a green-teal, which disappears over a basemap with a
lot of green landcover — the reason `MAP_TILE_URL` wants a muted, low-saturation style (§2 of
LOCAL.md). If a green-heavy basemap is ever the only option, move this band rather than fighting
it: the boundary is what carries meaning, the hue is free.

Below 0.01 - that is, at exactly zero - the pixel is fully transparent, so "no rain" and "no data"
both read as nothing drawn. The first band starts at the product's quantum (D-52), so every reading
the radar reports as non-zero is on the map.
The map is not the place to distinguish them; the staleness banner and the gap markers are.

**On the hourly column.** Rain intensity is conventionally classified in mm per *hour*, and RV
measures mm per five-minute interval, so the hourly figure is the 5-minute value × 12 — *if it
kept raining this hard for an hour*. That is the usual way radar intensities are labelled and it
is still an extrapolation: a shower that drops 6 mm in five minutes and then stops did not
deliver 72 mm. The names follow the conventional light / moderate / heavy classes those hourly
rates fall into, with DWD's own Starkregen warning thresholds (15–25 mm/h *markant*, 25–40 mm/h
*Unwetter*) landing in the top three bands.

The boundaries were chosen for the map first and the names fitted to them afterwards, not the
other way round — so they are a readable scale rather than a claim that 0.70 mm/5 min is a
recognised meteorological boundary.

**Where a subscriber's threshold sits.** The picker offers exactly these seven values. A stored
threshold that is not one of them — anything set through the API, or a row created before the
default moved to `0.15` —
is kept as its own option wearing the colour of the band it falls into, never snapped to a
neighbour: silently changing someone's threshold while showing them a settings page is worse
than an odd-looking dropdown.

### 11.2 Settings page (`/manage`)

Self-service, for the subscriber themselves. There is no admin view: nothing in this service
needs to read someone else's coordinates, and building a page that can is a GDPR liability
before it is a feature.

**Getting in.** The page has no idea who you are when it renders - the token is in the fragment,
so the request that fetched the page did not carry it. It asks. Three states, decided in the
browser:

1. No token, no session: a form asking which channel you signed up with.
2. A token in `#t=`: it is POSTed to `/manage/session`, spent, and replaced by a cookie. The
   fragment is erased with `replaceState` so Back does not return to a URL holding a spent token.
3. A session: the settings form.

**Since D-64, for push:** a browser holding a device key skips all three - every request it makes is
signed, so the page asks the API directly and there is no session. The states above are what is
left when there is no key (a subscriber from before the release, a browser that cannot keep one) and
for email. A link redeemed in such a browser registers a key, so the link step happens once.

`POST /manage/link` answers `202` whether or not the address is known, and sends nothing to an
**unconfirmed** subscriber - confirmation is what proves the channel reaches the person, and a
settings link is not the place to take that on trust.

**What can be changed, and the bounds** (D-28):

| Field | Range | Where the number comes from |
|---|---|---|
| `threshold_mm_5min` | 0.01 – 40.0 | RV's quantum (`PR E-02`) to `plausibility_max_mm_5min`. The page offers the seven bands of §11.1.1; the wider range is what the API accepts |
| `lead_time_minutes` | 5 – 120, step 5 | every lead RV carries; `rules.py` walks them in fives |
| `radius_m` | 0 – 20 000 | the `radius_sane` CHECK; under ~500 m it is one grid cell |

Each bound also exists as a CHECK constraint. The constraint is the guarantee; the validation is
so that a number someone typed becomes a sentence they can read, rather than an `IntegrityError`
and a 500. The `numeric(5,2)` column makes that concrete: `0.001` rounds to `0.00` in the column
and then fails `threshold_positive`, so the edge rounds and compares before the database sees it.

**The map.** Centred on the stored location at zoom 11 - roughly 40 km across, enough to see
which town you are in and to judge a radius of a few kilometres - and zoomable to 18, because the
basemap is what you orient by and street names are the difference between "somewhere in
Neuhausen" and "my street". `maxZoom` is 18 on the Leaflet map and 20 on the vector map (D-62); the radar overlay simply scales up past
~12 and goes blocky, which is honest about it being 1 km data. A draggable marker and a click
handler both write the coordinate
fields, rounded to the four decimals the server keeps so the field shows what will be stored.
The radius is a circle that resizes as the number changes. The current radar frame (t+0 only -
this page is for choosing a spot, the radar loop is for watching weather) is drawn underneath everything
else, because an overlay on top hides the thing being positioned.

Picking a spot and showing rain on it are separate capabilities: with no `OVERLAY_DIR` the map
still works, it just has no radar on it. Clearing `MAP_TILE_URL` removes the basemap too, and the
fallback is the same graticule-and-cities used by the radar.

**Saving** is two requests, not one, because a move resets the alert state and a rule change does
not (D-29). The rule goes first, so a refused rule does not leave the location already moved.

**How long a session lasts.** Thirty minutes from redeeming the link, **absolute** - nothing
slides it, and a page reload does not reset it. The page shows the time left and offers an
explicit *Verlängern* button, because a session that ends at a predictable moment is the
protection, and sliding on every request would quietly remove it while looking like a courtesy.

Renewal stops at a wall (`manage_session_max_minutes`, two hours) measured from when the link
was spent, so the button cannot turn a deliberately short session into a permanent one. The wall
is carried **inside the signed token**, which is the only record of when the session began - it
needs no session table, and a holder who could edit it could renew forever.

The CSRF value is minted against the session's own expiry, not a lifetime of its own. Given its
own clock the two drift: until 2026-09-21 every page load minted a fresh thirty minutes for the
CSRF token while the session's expiry stayed put, so it could outlive what it belonged to.
Harmless, because the session is checked first - but two things that are meant to be one.

**The cookie is signed, not encrypted.** Its holder can read it, copy it to another browser and
delete it; they cannot alter it. Everything before the MAC is inside the MAC, so pushing the
expiry out, moving the wall, swapping the subscriber id or promoting a session token to a CSRF
token all fail verification. There is a test that tries each.

**Ending the session** is a link below the form, not a button beside Save. Two reasons: next to a
submit button anything button-shaped reads as Cancel, and "Abmelden" in German means both "log
out" and "cancel my subscription" - on a page with a subscription on it, that is the one word to
avoid. It says "Sitzung auf diesem Gerät beenden - die Warnungen laufen weiter".

### 11.3 Browser geolocation

One helper, `static/geolocate.js`, used by all three pages that offer to find you. It is a served
file rather than three inline copies because the interesting part is the error handling, and
error handling duplicated three times is error handling that will differ three ways.

**Every way the API declines arrives on the error callback**, which is why the first version of
the subscribe button appeared to do nothing: it passed a success callback and nothing else. The
helper handles all of them, plus two the API does not report:

| Case | What the person is told |
|---|---|
| Insecure origin | needs https or localhost - and says the coordinates can be typed |
| No API at all | same, without the https advice |
| `PERMISSION_DENIED` | the browser refused; it can be re-allowed in site settings |
| `POSITION_UNAVAILABLE` | could not be determined, try again |
| `TIMEOUT` | took too long, try again |
| Outside `LAT_RANGE`/`LON_RANGE` | the service only covers Germany |

The last is checked here as well as at the API so that someone abroad is told why, rather than
having a coordinate filled in for them that the server then refuses.

**The secure-context rule deserves its own note** because it is the one that looks like a bug.
Over plain http on anything but `localhost`, `navigator.geolocation` still exists - so guarding
on its presence passes - and the call fails with `PERMISSION_DENIED`, which without care is shown
as "you declined" to someone who was never asked. The helper checks `isSecureContext` first and
says what is actually wrong. No page code can do better; the remedy is the origin.

A `timeout` is set for the same family of reasons: with none, the callback may simply never
arrive - a headless browser with no location provider does exactly that - and the button stays
disabled forever, which is the original silent failure wearing a different hat.

On the radar this is an on-map control in the top-left under the zoom buttons, styled as a
`leaflet-bar` so it looks like what it is. It draws the position as a dot **and an accuracy
circle**: at 1 km radar scale, "here" and "somewhere within 2 km" look identical, and only one of
them is true.

## 12. Notifications

```python
class Notifier(Protocol):
    def send(self, message: OutboundMessage) -> DeliveryResult: ...
```

Adapters: `console` (dev, prints), `smtp` (generic), `brevo`/`mailgun`/`sendgrid` (HTTP API, default
in prod), `push` (**stub**, raises `NotImplementedError` — D-15). Selected by `NOTIFIER` env var.

Alert mail:
- Subject: `Regen in ~20 Minuten (Musterstadt)` — lead time and a place name resolved once at
  subscribe time (reverse geocode is optional; falls back to coordinates).
- Body (text + minimal HTML): predicted start time (local), expected intensity class, the 30-minute
  series in a compact line, the map link, the data timestamp, the DWD attribution, and the
  unsubscribe link.
- Headers: `List-Unsubscribe` (the same fragment link as in the body) and
  `Auto-Submitted: auto-generated`. **Not** `List-Unsubscribe-Post`: see D-33 for why one-click
  was removed rather than fixed, and Q-13 for what bringing it back would take.
- Tap target (push only, `click_url`): `/#l=<locate token>` — the radar, opened on the place
  the warning was about (D-38). The coordinates are **not** in the link: a warning stays in a
  notification list for good, and a screenshot of one carrying decimal degrees would say more
  than the message does, which names a time and an intensity but never a place. The reference
  stops resolving after `LOCATE_LINK_TTL_MINUTES` and the map then opens on the country view.
  Not put in the mail body: email ignores `click_url`, and a body is forwarded far more often
  than a notification is.
- Deliverability: SPF + DKIM + DMARC on the sending domain are a **hard prerequisite** for M6; without
  them these mails land in spam and the whole service is pointless.

**Push (W3C Web Push), replaced ntfy 2026-09-27 (D-45).** `NOTIFIER=auto` posts an encrypted
notification to the push endpoint a browser issued, instead of sending mail. The original reason
holds and is about the product rather than convenience: a warning is only useful before the rain,
email latency is unpredictable - usually seconds, sometimes minutes, and greylisting can cost five -
and a fifteen-minute lead time does not survive that.

*Why it replaced ntfy.* Not privacy, which is where the question started, but comprehensibility. The
first thing a subscriber had to do was install an app with no visible connection to rain, and no
wording on the page could fix that. On Android web push needs no install: Chrome delivers to an
ordinary tab, so the step disappears rather than being explained. The privacy improvement came
along for free - ntfy.sh saw the plaintext of every warning, and a push service sees ciphertext.

*The endpoint is issued, never chosen - and never trusted.* A topic was a name we generated because
a guessable one would leak a location. An endpoint needs no such care: the browser mints it and
shows it to nobody. It brings the opposite problem instead. It is a URL that arrives in a request
and that this service then POSTs to, which unchecked is a server-side request forgery primitive
aimed at the metadata server or anything else the container can reach. `ALLOWED_PUSH_HOSTS` in
`notify/webpush.py` is the answer, applied at subscribe time so the row never exists and again
before every send so a row that arrived another way still cannot become an arbitrary request.

*What the push service sees.* The payload is encrypted to keys only that browser holds (RFC 8291,
`aes128gcm`), so Google, Mozilla or Apple cannot read the warning. What they do see, and what no
encryption can hide, is the endpoint, the timing and the size of each message. Timing is not
nothing: a notification arriving says it is about to rain where this subscriber is, which is a
coarse signal about their location. The location itself is never transmitted.

*Confirmation still happens, for the reason it always did.* On email it proves the person filling
the form controls the address, which is what stops the service being a mail relay. On web push there
is no third party to protect - the browser handed us its own endpoint. What the confirmation proves
instead is that the channel reaches them, and there is *more* of that chain to get wrong here, not
less: a service worker that fails to install, a payload the browser refuses, a permission granted
and revoked before the first send. Each leaves a subscription that looks healthy from the database
and shows nothing on the device. A warning that silently goes nowhere is worse than none.

*What transience cost.* A notification is gone when it is swiped, and Android keeps no history by
default. That killed the anchor message - the one the reader was asked to keep, which worked only
because the ntfy app held a scrollable list - and moved the way out onto the settings page, reached
by the Einstellungen button on every warning. `Notification.maxActions` is 2 where ntfy rendered 3,
which is enough: nothing sends more than one. And the subscription dies with the browser's site
data, which is D-47.

*Leaving without telling us.* Blocking notifications, clearing site data and uninstalling the
browser all revoke the subscription, and none of them reaches this service. A 404 or 410 from the
push service is the only signal, and it arrives only when something is sent - so the weekly
liveness notification exists to make sure something eventually is (D-46).

Transactional mails: confirmation (double opt-in), deletion confirmation. No marketing mail, ever.

---

## 13. Security, privacy, GDPR

The service processes **email address + precise location** of identifiable people in Germany. Even as
a private project this is personal data under GDPR.

- **Legal basis:** consent (Art. 6(1)(a)), obtained via double opt-in; the confirmation timestamp and
  source IP hash are the consent record.
- **Data minimisation:** store only email, coordinates, rule settings, and the alerts actually sent.
  **Coordinates are rounded to four decimals (~11 m) at the API edge**, in the request models, so
  no route can store more by accident. The service samples a radius mask on a 1 km radar grid, so
  even 100 m cannot change an answer - the seven decimals a phone reports, or the six a mapping
  site copies, are precise personal location data that nothing here reads. This was a browser-side
  `step="0.0001"` until 2026-09-18, which enforced nothing against a caller who skipped the form
  and rejected legitimate pastes as invalid.
  No location history (the location is overwritten in place — D-16), no IP logs beyond a hashed value
  for rate limiting, retained 7 days. The per-cycle `evaluations` log is purged after 48 h (D-23): an
  indefinite 5-minute-resolution series per subscriber would be a presence log, which is more personal
  data than this service has any reason to hold.
- **Purpose limitation:** the stored evaluation rows contain coordinates-derived samples only, never
  raw identifying data beyond the subscription id.
- **Deletion:** `DELETE /api/v1/subscriptions/me` and the unsubscribe link both hard-delete
  (cascade). Unconfirmed subscriptions are purged after 24 h; subscriptions with no location update
  and no login for 12 months are purged after a warning mail.
- **Transport:** HTTPS only, HSTS, secure cookies (`SameSite=Lax`), CSP without inline scripts except
  a nonce for the map bootstrap.
- **Tokens:** 32 bytes from `secrets.token_urlsafe`, stored only as SHA-256; confirm tokens single-use
  with 24 h expiry; api tokens revocable; constant-time comparison.
- **Abuse:** subscribe endpoint is rate limited and only ever sends mail to an address after the
  double opt-in, so it cannot be used as a mail relay.
- **Secrets** in Secret Manager, never in the repo; `.env.example` holds names only.
- **Logging:** never log full email addresses or exact coordinates; log `subscriber_id` and
  coordinates rounded to 2 decimals (~1 km) instead.
- **Consent text is versioned and per channel.** `consent_text_version` records which wording was
  agreed to, so a record still means something after the text is edited (Art. 7(1)) - it must be
  bumped whenever that text changes. Since M5 there are **two wordings per version**, one naming
  an email address and one a push topic, because they describe different data; the subscriber's
  `channel` is stored alongside, so channel + version identifies exactly what was on screen
  without a second column. The signup note also stopped claiming that nothing is stored before
  confirmation: a `pending` row exists from the moment of signup and is deleted after
  `unconfirmed_purge_hours`, which is what the privacy page had always said.

**Deferred until a public launch (D-18):** Impressum (§5 TMG/DDG), full Datenschutzerklärung,
Auftragsverarbeitungsvertrag with the mail provider, a cookie/consent banner if third-party tiles are
used. Tracked as **Q-2**.

---

## 14. Configuration

All configuration via environment variables, parsed by a single pydantic `Settings` object.

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | — | Postgres DSN |
| `GCS_BUCKET` | — | archives + overlays |
| `DWD_BASE_URL` | `https://opendata.dwd.de/weather/radar/composite/rv/` | product directory |
| `DWD_USER_AGENT` | — | must include contact URL/mailbox (§4.3) |
| `DWD_MAX_ATTEMPTS` | `5` | per cycle |
| `DWD_DAILY_BYTE_BUDGET` | `8589934592` | 8 GiB guard; halting pages the operator (§4.3 rule 7) |
| `DWD_HOURLY_BYTE_BUDGET` | `536870912` | 512 MiB guard, so exhaustion costs an hour not a day |
| `DWD_MAX_RESPONSE_BYTES` | `33554432` | 32 MiB hard cap on a single response (§4.3.1) |
| `CYCLE_MAX_FUTURE_MINUTES` | `15` | reject a cycle stamped further ahead than this |
| `CYCLE_MAX_AGE_HOURS` | `3` | reject a cycle stamped further back than this |
| `PLAUSIBILITY_MAX_MM_5MIN` | `40` | national max above this gates the cycle |
| `BLAST_RADIUS_MAX` | `25` | absolute cap on subscriptions warned in one cycle (§9) |
| `RAW_RETENTION_HOURS` | `48` | GCS lifecycle (D-7) |
| `EVALUATION_RETENTION_HOURS` | `48` | purge of the `evaluations` debug log (D-23) |
| `OVERLAY_OBS_RETENTION_HOURS` | `14` | GCS lifecycle, observed frames (12 h window + margin) |
| `OVERLAY_FC_RETENTION_HOURS` | `1` | GCS lifecycle, forecast frames |
| `TIMELINE_PAST_HOURS` | `12` | default/maximum past span of the slider (D-22) |
| `DEFAULT_RADIUS_M` | `2000` | D-3 |
| `DEFAULT_THRESHOLD_MM_5MIN` | `0.1` | D-13 |
| `DEFAULT_LEAD_MINUTES` | `30` | D-13 |
| `DEFAULT_MIN_GAP_MINUTES` | `0` | D-9/D-10 — off |
| `DRY_CLEAR_MINUTES` | `30` | `RAINING → DRY` |
| `WARNED_RETRACT_CYCLES` | `3` | `WARNED → DRY` |
| `MISSING_FRACTION_LIMIT` | `0.30` | data quality gate |
| `NOTIFIER` | `console` | `console｜smtp｜brevo｜mailgun｜sendgrid｜push` |
| `MAIL_FROM`, `MAIL_API_KEY` | — | provider credentials |
| `PUBLIC_BASE_URL` | — | link generation |
| `SECRET_KEY` | — | token/HMAC signing |
| `LOG_LEVEL` | `INFO` | structured JSON logs |

---

## 15. Observability

Metrics (Prometheus text on `/metrics`, mirrored to Cloud Monitoring):
- `rainalert_cycle_age_seconds` — **the key SLI**: now − latest `radar_cycles.nominal_time`.
  Alert if > 20 min. Note this value derives from a field **the remote party supplies**, so it is
  clamped at zero and a **negative raw age is its own paging condition**: a cycle stamped in the
  future would otherwise read as "the freshest data we ever had" and silence this alert until real
  time caught up. §4.3.1 rule 5 rejects such a cycle at ingest; this alert is the backstop for
  whatever slips through.
- `rainalert_cycle_status_total{status=ok|partial|rejected}` — a cycle can be fetched, stored and
  *meaningless*. Staleness monitoring does not cover "fetched, parsed, and implausible", which is
  exactly the shape of a poisoned-upstream attack that suppresses everyone's alerts while the
  dashboard stays green.
- `rainalert_fetch_attempts_total{result=ok|notmodified|late|failed}`
- `rainalert_fetch_bytes_total`, `rainalert_daily_budget_used_ratio`
- `rainalert_decode_seconds`, `rainalert_pipeline_seconds` (alert if > 120 s — the 5-minute budget)
- `rainalert_subscriptions_active`
- `rainalert_evaluations_total{decision=…}`
- `rainalert_notifications_total{status=…}`
- `rainalert_missing_fraction` histogram (radar outage visibility)
- `rainalert_timeline_gaps` — cycles missing from the last `TIMELINE_PAST_HOURS`; a non-zero value
  means the map shows holes

Logs: structured JSON, one summary record per cycle (nominal time, bytes, decode ms, subscriptions
evaluated, alerts queued/sent, skipped). A per-subscription debug log line is emitted only at
`DEBUG`.

Operational alerts (email to the operator): cycle age > 20 min, circuit breaker open, notification
failure rate > 20 % over 30 min, daily budget > 80 %.

---

## 16. Testing strategy

Unit tests must run **offline** and fast. No test ever touches `opendata.dwd.de`.

1. **Decoder golden test.** Fixture `tests/fixtures/DE1200_RV2609161355_trimmed.tar.bz2` (frames
   `_000`, `_060`, `_120` of the 2026-09-16 13:55 cycle). Assert the decoder's output equals
   `wradlib.io.read_radolan_composite` on the same file (wradlib is a test-only dep, D-21) — this has
   already been verified bit-identical once by hand, so a failure means real drift.
   Note wradlib returns `-9999.0` for no-data, **not** `NaN`, and exposes `meta['nodatamask']` as flat
   indices; compare against those. Assert member naming and header parsing, **not** "25 members" —
   the fixture holds three.
   Include the sentinel case explicitly: a cell of `0x29C4` must decode to missing, never to
   25.00 mm/5 min (§5).
1b. **Radar-dropout test.** Fixture `tests/fixtures/DE1200_RV_outage_20260915_1615-1630.tar.bz2` — a
   real two-cycle dropout of the Borkum radar. Assert, for a 2 km mask at 53.58 N 6.66 E: at 16:15
   `missing_fraction[0] == 0` but the forecast frames are fully missing, so the cycle is **not**
   evaluated as dry; at 16:20 and 16:25 every frame is missing and the state is left unchanged; at
   16:30 normal evaluation resumes. Hamburg in the same files must be unaffected throughout.
   *This is the regression test for the frame-0-only gate — a real case that defeats it.*
2. **Grid/georeferencing test.** Reference points against `wradlib.georef.get_radolan_grid`,
   tolerance half a cell; plus known city coordinates.
3. **State machine tests.** Table-driven over synthetic sequences: clean onset, showers, forecast
   retraction, radar outage mid-warning, location jump, quiet hours on/off, `min_gap` on/off. Every
   row of the §9 table has at least one test.
4. **Sampler tests.** Synthetic grids with a known blob; assert radius mask size and max semantics;
   out-of-coverage handling.
5. **Politeness tests.** HTTP mock asserting: one GET per cycle, conditional headers sent, backoff
   sequence on 429/503, attempt cap, circuit breaker, budget guard.
6. **Idempotency test.** Run the pipeline twice on the same cycle → one `radar_cycles` row, one
   `evaluations` row per subscription, one mail.
7. **API tests.** Double opt-in flow end to end with the console notifier; token expiry/single use;
   enumeration-safe responses; deletion cascades.
8. **Rendering test.** Overlay PNG has expected size/bounds; colour scale maps known values;
   analysis and forecast frames land under the right prefixes.
9. **Timeline manifest test.** Frames ordered by `offset_minutes`; exactly one observed frame per
   cycle; forecasts only from the latest cycle; `kind` correct either side of the *now* boundary;
   missing cycles reported in `gaps` rather than silently skipped; `past_hours` clamped to
   `TIMELINE_PAST_HOURS`.
10. **Manual acceptance.** `rainalert probe --lat --lon` CLI prints the 25 lead values for a point;
   compare against a public radar map during actual rain before trusting the alerts.

CI: ruff + mypy (strict on `rainalert/`) + pytest + the import-linter rule that runtime code never
imports wradlib. Container image built and smoke-tested (`--help` on both entrypoints).

---

## 17. Repository layout

```
RainForecastWarning/
  pyproject.toml            # uv/poetry; groups: runtime, dev(+wradlib), test
  Dockerfile                # one image, entrypoints: api | ingest | cli
  Makefile                  # dev, test, lint, run-api, run-ingest, fixtures
  docs/DESIGN.md            # this document
  docs/DWD_RV_FORMAT.md     # M0 spike findings (authoritative once written)
  infra/                    # terraform or gcloud scripts, GCS lifecycle json
  migrations/               # alembic
  rainalert/
    config.py               # pydantic Settings (§14)
    db/                     # models, session, repositories
    radar/
      client.py             # politeness-enforcing DWD HTTP client (§4.3)
      decoder.py            # header + payload → arrays (§5)
      grid.py               # DE1200 ↔ WGS84, radius masks (§5)
      overlay.py            # reprojection + PNG rendering, obs/fc split (§11.1)
    alerting/
      sampler.py            # §8
      rules.py              # threshold/lead evaluation
      state_machine.py      # §9 — pure functions, no I/O
      dispatcher.py         # queue + deliver
    notify/
      base.py console.py smtp.py brevo.py push.py   # §12
    api/
      main.py routes_*.py templates/ static/
    jobs/
      ingest.py             # the Cloud Run job entrypoint (§6)
      retention.py          # purge job (GCS lifecycle + evaluations TTL)
      backfill.py           # past cycles from DWD / re-render from raw archives (§11.1)
      verify.py             # prediction vs observation (§9)
    cli.py                  # probe / backfill / render-backfill / send-test-mail
  tests/
    fixtures/ unit/ integration/
```

`state_machine.py` and `rules.py` must be **pure** (no DB, no clock, no network — time is injected).
That is what makes §16.3 cheap to write and trustworthy.

---

## 18. Milestones

Each milestone ends with a working, demonstrable artefact.

**M0 — DWD RV spike.** ✅ *done 2026-09-16*
Download a real RV archive by hand. Document in `docs/DWD_RV_FORMAT.md`: exact `_LATEST` filename,
inner member names and count, every header field with an example, the `PR` precision value, the flag
bit meanings, file size, observed publication delay over a couple of hours, and **how far back the
`rv/` directory retains files** (this decides whether the 12 h timeline can be backfilled at deploy
time or only accrues — §11.1).
*Done when:* the doc exists and a trimmed fixture is committed.
*This resolves every "VERIFY" marker in this document; if reality differs from §4.1, update §4/§5
before writing code.*

**M1 — decoder + grid + CLI.** ✅ *done 2026-09-16*
`decoder.py`, `grid.py`, `rainalert probe --lat 50.1 --lon 8.7` prints the lead values with valid
times. Tests §16.1, §16.2, §16.4 plus the dropout regression (§16.1b) — 32 tests, lint clean.
Grid verified against wradlib over all 1 320 000 cells.
*Acceptance criterion met 2026-09-18.* Probe output was checked against DWD's own radar display
during real rain at 48.1891 N 12.8532 E, cycle 11:05 UTC: 25 frames, 17 sites reporting, full
coverage, raining at +0 (0.17 mm/5 min) and tapering to nothing by +50. The two independent things
this confirms are the grid and the clock - the point resolved to row 273, col 769 and the values
there match what DWD draws over that spot, and 11:05 UTC printed as 13:05 local. The fixtures had
only ever proved internal consistency.

**M2 — ingest pipeline.** ✅ *deployed; running against the real DWD server on a 5-minute schedule, confirmed 2026-10-04*
Politeness client, archiving, `radar_cycles`, idempotency, advisory lock, the §4.3.1 validation
gates, retention. `make run-ingest` runs one cycle; 76 tests pass, including 14 politeness tests and
an ingest suite against a real Postgres.
*Done when:* 24 h of unattended running produces exactly 288 cycle rows, zero duplicate downloads,
and the politeness tests pass.
*First live run: 2026-09-18.* `make run-ingest` against the real `opendata.dwd.de` returned 200
and stored a 25-frame cycle in one attempt - the decoder, the politeness rules and the nominal-time
arithmetic all met the live product for the first time and held. Two bugs surfaced that no test
had: `LocalArchiveStore` could not take the relative `ARCHIVE_DIR` the documentation prescribes
(every fixture used an absolute `tmp_path`), and `make probe` was hardwired to a test fixture.
*Outstanding:* the 24 h run - 288 cycle rows, zero duplicate downloads. **Watch the first few
cycles rather than scheduling it and walking away.**
`docs/LOCAL.md` closes this without deploying — a laptop can reach DWD, and an afternoon of
`make run-ingest` on a five-minute loop exercises the same path the Cloud Run job will, including
the first real 25-frame archive the decoder has ever seen.
*Deliberately deferred:* Prometheus metrics (the endpoint belongs to the API service in M3; for now
the per-cycle numbers go to structured logs); the GCS store is written but unexercised, there being
no bucket yet (M6); Alembic arrives with M3, when there is more than one table to migrate.

**M3 — subscriptions + mail.** ✅ *deployed. Push rather than mail is the live channel; the mail path is code-complete and unexercised (no `smtp_host` configured)*
Schema and Alembic migrations, API (§10) minus `/forecast` and `/overlays`, double opt-in,
subscribe/confirm/unsubscribe/privacy pages, `Notifier` adapters, rate limits, deletion. 98 tests.
Verified end to end against a live server: subscribe → confirmation mail → GET leaves the state
untouched → POST activates → API token works → unsubscribe deletes everything.
*Done when:* a friend can subscribe from a phone browser and unsubscribe with one click.
*Outstanding:* a real address on a real domain, which needs Q-1 (domain) and Q-4 (provider), plus
SPF/DKIM/DMARC. Until then the flow is exercised with the `file` notifier.
*Deliberately deferred:* the map picker is M5, so the subscribe form takes coordinates with a
browser-geolocation button; rule parameters exist as columns and API fields but are not in the UI
(D-14); `PATCH /subscriptions/me` and pause/resume are not implemented yet, and neither is the
`/manage` page - changing a location is an API call until it is.

**Delivery is configuration, not code (Q-4).** Every provider worth using — Brevo, Mailgun,
SendGrid, Postmark, SES, or an ordinary mailbox — speaks SMTP, so the SMTP adapter covers all of
them and choosing one is a matter of host, port and credentials. A provider's HTTP API can be added
behind the same `Notifier` protocol later if its delivery telemetry justifies the coupling. The
`console` and `file` adapters make the whole opt-in flow testable without sending anything.

**Domains are configuration too (Q-1).** `PUBLIC_BASE_URL` and `MAIL_FROM` are the only places a
hostname appears; every link in every mail is built from the former. The defaults are working
localhost placeholders, so nothing is blocked on choosing a domain.

**M4 — alerting.** ✅ *deployed 2026-10. Caps from F-15/F-2 landed 2026-10-04*
Sampler, rules, state machine, dispatcher, `evaluations`/`rain_events`/`notifications`, and the
verification job. 143 tests, including the §9 transition table row by row.
Verified end to end: a real DWD cycle, a subscriber at a point that is dry now and wet in an hour,
produced one warning mail with a working one-click unsubscribe - and a second cycle of the same
front produced none.
*Done when:* a real alert mail arrives before real rain, and a replay of a stored rainy day produces
exactly one mail per event.
*Outstanding:* the real-rain half needs live DWD access and a real mailbox. The replay half is
covered by tests but not yet by an actual stored day.
*Deliberately deferred:* `dry_clear_minutes` is a constant rather than per-subscription; the
onset-field optimisation stays unbuilt (§8.1).

**On `UNKNOWN`:** an earlier revision had the first cycle only *observe*, which left every new
subscription — and every subscription that had just moved — blind for five minutes. It now warns
immediately when the first observation is dry with rain approaching, because that is a complete
picture: we know it is not raining here and we know rain is coming. Only the `now_wet` case stays
silent, which is the case that matters — rain already falling tells us nothing about whether the
subscriber has just walked into it.

**M5 — map UI.** ✅ *deployed. Merged into `/` 2026-09-29 (D-48); `/map` deleted*
Overlay renderer (obs + fc prefixes), `/api/v1/overlays/timeline`, re-render job, the radar page with
the slider, staleness banner, gap rendering, legend and attribution. 164 tests.
Verified against real data: the rendered overlay agrees with the source grid at six German cities
(6/6), and the manifest labels observed and forecast frames, reports gaps, and flags staleness.
*Measured, better than the estimate:* a frame is **24 KB**, not the ~150 KB assumed, because most
of the image is transparent - a full 168-frame timeline is ~4 MB rather than ~25 MB, and one
cycle's overlays are ~90 KB. The windowed loading stays anyway: it is still the right shape on a
phone, and it is what keeps a slider drag from fetching 168 images at once.
*Done when:* the slider animates -12 h ... +2 h smoothly on a phone over mobile data, gaps render
as gaps, and the observed/forecast boundary is unmistakable.
*Outstanding:* the phone half - the page has been driven by HTTP, not by a thumb on a real device.
*Deliberately deferred:* the subscribe form still takes coordinates with a geolocation button
rather than a draggable map marker; the timeline is the map feature that earns its keep first.

**M6 — deploy.** ✅ *applied. Live at `https://rainalerts.web.app` behind Firebase Hosting (D-49 region note), 2026-10*
Terraform for all §6.1 resources (project `rainchecker-195519`, `europe-west3`), Secret Manager,
Cloud Run service + ingest job + migrate job, Cloud Scheduler at `4-59/5`, two buckets with
lifecycle rules, monitoring alerts, `Dockerfile`, CI, and `docs/RUNBOOK.md`.
*Done when:* the service has run unattended for a week with cycle age < 20 min at all times.
*Not yet demonstrated* — but it is now measured rather than hoped for: `stale_radar` alerts on
exactly that condition (D-50), so a week without that email is the evidence.

*Decisions:* Terraform rather than shell scripts; CI runs lint and tests only, deploys are by hand
from `make image-push` plus `terraform apply`.

*What is verified and what is not.* The HCL parses, every security-critical setting the code reads
is set by Terraform (cross-checked against the `Settings` model), and the image's dependency set is
proven complete by installing the package into a clean environment and importing every runtime
module. **Neither `docker` nor `terraform` can run in the development environment**, so the image
has never been built and the plan has never been rendered. Expect the first `terraform apply` and
the first build to need fixing; they are not "verified working".

*Still blocked on prerequisites*, in the order they bite: the domain (Q-1), the mail provider and
its credentials (Q-4), SPF/DKIM/DMARC on the sending domain, and the provider's DPA (Q-9). Only
the last is a legal rather than technical blocker, and it applies from the first friend's address.

*Known gaps, listed in the runbook:* the cycle-age SLI is not on a Cloud Monitoring *dashboard* (it
lives in the database, which Monitoring cannot see) — but it is alerted, by logging it from the
ingest job and filtering the words (D-50); the map tiles are still third-party, under the OSMF's vector tile policy (best effort, no SLA, may block without notice; D-61) - the OSMF's vector server, which sees each visitor's IP and map area, disclosed on the privacy page (D-59); the map libraries are vendored; Cloud Run's
request logs still carry confirm and unsubscribe tokens for 30 days by default.

**M7 — later (not v1).** Mobile app + FCM/APNs, user-editable rule UI, "all clear" mails, multiple
locations per subscriber, additional countries/sources.

---

## 18.1 Security findings still outstanding

An independent adversarial review is at [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md) — 18 findings,
6 high, none critical. The findings that were design-level have been folded into the sections above
(§4.3.1, §6.1, §8.0, §8 step 4, §9, §15) and the two that were live code (F-1 decompression bomb,
F-3 undocumented exception types) are fixed with regression tests in `tests/test_hostile_input.py`
and `tests/test_grid.py`. The rest are tracked here so they are not lost, against the milestone that
owns them:

| Finding | Owner | Note |
|---|---|---|
| F-4 long-lived API token issued by an emailed `GET` link | M3 | Mail scanners GET links: the scanner consumes the single-use token *and* receives the bearer token. Make confirm a `POST` from a landing page; never put a token in a URL that gets logged |
| F-5 rate limiting has no defined client-IP source | M3 | `X-Forwarded-For` is attacker-controlled unless the trusted-proxy hop count is pinned. Deletion also erases the abuse state, so delete-and-retry resets any limit |
| F-6 scale-to-zero cost/DoS | M6 | Folded into §6.1 as `--max-instances`; the reserved-connection half is deploy work |
| F-8 Cloud Run request logs defeat §13 | M6 | The platform logs full URLs including query strings for 30 days by default — §13's "plaintext never stored" is only true once that is configured |
| F-9 "public-read or proxied" also exposes `raw/` | M6 | Split the bucket, or proxy; the raw DWD archives should not be world-readable next to the overlays |
| F-12 GDPR: DPA scope, consent columns, reversible IP hash | **now** | The mail-provider Auftragsverarbeitungsvertrag applies from the first friend's address, not from public launch. §7 has no columns for the consent record. An unsalted hash of an IP is reversible by brute force |
| F-13 `/metrics` "internal" is not expressible on Cloud Run | M6 | Either authenticate it or do not expose it |
| F-14–F-16 `/forecast` amplification, rule-parameter abuse, web hardening | M3/M5 | CSP `frame-ancestors`, `Referrer-Policy`, CSRF, mail header injection, session model |
| F-17 location updates silently suppress alerting for a moving user | M7 | D-17 resets state on a >1 km move; an app updating location often could keep a user permanently in `UNKNOWN` |
| F-18 supply chain and deploy path | M6 | Pin dependencies, pin base image by digest |
| **The push endpoint is a URL an attacker supplies, and we POST to it** | M7 | *New with D-45.* Without a host check the subscribe API is a server-side request forgery primitive: `endpoint` is fetched by this service from inside its own network, so `https://169.254.169.254/computeMetadata/v1/...` would be retrieved on the caller's behalf and its status handed back through `notifications.error`. Mitigated by `ALLOWED_PUSH_HOSTS` and `check_endpoint` in `notify/webpush.py`: https only, port 443 only, and the host must be one of six known push services or a subdomain of one. Enforced twice - in `subscriptions.subscribe` so the row never exists, and in the notifier so a row that arrived by a migration or a fixture still cannot become an arbitrary request - and `httpx` is constructed with `follow_redirects=False` so a 302 from a real push service cannot walk out of the allowlist. Bypass attempts refused under review: fragment smuggling, trailing dot, tab injection, IPv4-mapped IPv6, punycode, scheme-relative, suffix-without-dot. Two holes were found by review rather than by design and are now closed. The port check: the host allowlist alone let `fcm.googleapis.com:22` through - not an internal SSRF, but it lets a stranger spend a 10 s connect timeout per send. And **userinfo**, which an earlier version of this row wrongly listed as already refused: `https://evil.test@fcm.googleapis.com/...` passed the hostname check and then httpx turned the userinfo into an `Authorization: Basic` header that *replaced* the VAPID one, so the push service saw no VAPID at all and refused every send - a row that can never be delivered to. Also a reminder that a claim about what a check refuses belongs next to a test, not in prose |
| ~~**A rotation endpoint authorised by the old push endpoint**~~ | M7 | *Introduced and removed on 2026-09-27, before it ever shipped.* `POST /api/v1/push/resubscribe` moved a confirmed subscriber to a new endpoint on proof of holding the old endpoint string, documented here as "the same capability the endpoint already is". **That premise was false.** A push service rejects a send whose VAPID signature does not match the key the subscription was created with, so knowing an endpoint lets a third party do nothing - it is a username, not a password. The endpoint therefore created capability from a bearer string: redirect a stranger's warnings to an endpoint of your choosing, and each warning carries a `#l=` locate reference that `POST /api/v1/locate` trades for exact coordinates, plus a request token that opens their settings. Demonstrated end to end in review. Removed rather than patched: it defended against `pushsubscriptionchange`, which Firefox fires with neither subscription populated - so the handler could not recover the signing key in the cases it existed for - and 410 pruning already covers rotation at the cost D-47 accepts. Any replacement must have the *new* subscription prove it is the same browser; no string the client already holds can do that |
| **A missing VAPID key took the whole site down** | M7 | *Found by review, 2026-09-28.* `create_app` builds the notifier before anything else, and `build_notifier("auto")` constructed a `WebPushNotifier`, which raises on an empty or malformed key. So on `NOTIFIER=auto` - what `infra/run.tf` sets - a VAPID secret that was blank, disabled, or unreadable by the runtime service account, or a revision deployed before the secret existed, made the process raise at import. On Cloud Run that is a crash loop in which no revision becomes ready: the map, the radar, `/privacy` and the **email** channel all down because one channel was misconfigured. `api/app.py` already carried a comment saying exactly that must not happen - *"a service that refuses to start because one channel is misconfigured takes the others down with it"* - one screen below the code that made it happen. `auto` now builds what it can, logs the rest, and omits the transport; `RoutingNotifier` already refuses per *message*, so a push subscriber's warning fails and is recorded as failed while nobody else notices. A test that had pinned the old fail-fast behaviour was inverted, with the reasoning written into it, because the previous round got this trade backwards |
| **A single `Location` header could roll back a whole liveness run** | M7 | *Found by review, 2026-09-28.* `run_liveness` accumulates every status change and every `session.delete(subscriber)` and commits once. `provider_message_id` was written untruncated into a `String(256)` - the dispatcher truncates, this did not - so an over-long `Location` from one push service raises `DataError` out of that commit and discards the entire run: every `sent_at` (so those subscribers are due again next week and get a second ping the message itself promises they will not) and every deletion for a subscription that had just returned 410. The retention mechanism D-46 exists for would stop working, and keep failing weekly for as long as that one endpoint stayed in the due set. The general shape is worth more than the fix: a job that commits once has a blast radius of the whole job, so every unbounded third-party value in it is a transaction-level risk, not a row-level one |
| **The service-worker harness could not see its own seam** | M7 | *Found by review, 2026-09-28.* Round 2 replaced grep-style tests with a real worker harness, and this round asked whether the harness could be fooled. It could: dropping `data: data` from `showNotification` left **all 17 cases green** while a body tap opened the signup form instead of the map and an Einstellungen tap did nothing - because every `notificationclick` case hand-fed `notification.data` and nothing asserted the `push` handler ever stored it. The same bug class as the original "`actions` computed and never passed", one field over, in the suite built to catch that class. Of ten mutations the reviewer tried, seven survived. Now 24 cases, including one that fires a real push and feeds the resulting notification's own `data` into the click handler, and the harness models `matchAll` options, a `navigate()` that rejects (as it does for an uncontrolled client), and a client without `focus()`. All nine re-tested mutations are caught. The lesson, third variation: a test that constructs the input it is meant to be checking proves only that the assertion runs |
| **A settings-link flood** | M7 | *Found by review, 2026-09-28.* `POST /api/v1/manage/link` limited per-IP only, while `POST /api/v1/subscriptions` limits per-IP **and** per-address - and `manage_link_limit_per_hour` documented itself as "deliberately as tight as signing up: it is the same mail-bomb lever as POST /subscriptions". It was not as tight, and the absent half was the one that survives IP rotation. Demonstrated: 40 POSTs with 40 values in `X-Forwarded-For`, all 202, 40 messages delivered to one subscriber. The flood is the smaller harm. `issue_manage_token` deletes the subscriber's previous *unused* token, so a stranger who knows an address could invalidate that person's real settings link as fast as they could request one - and for a push subscriber the settings page is the only route to "Abmelden und meine Daten löschen". **That last sentence overstated it, and the correction is worth keeping:** review demonstrated that because the rate-limit bucket and the delivery target are both derived from the same typed address, an attacker cannot burn the bucket while sending the link somewhere else - all five messages land in the owner's own mailbox, so for email this is a mailbomb plus a race against an in-flight link, not a denial of access. And for web push it needs the endpoint, which is a browser secret and is not enumerable, so the deletion-right scenario needs a precondition an attacker essentially cannot get. The flood was the real defect. Fixed by mirroring subscribe's per-address limiter, keyed on `hash_address` so an unknown address is counted and refused identically and the route stays oracle-free. **The residual, stated rather than hidden:** the fix converts an unbounded flood into a bounded lockout - someone who knows an address can now spend that address's five links per hour and leave the owner with a 429. Review attacked that trade specifically and found it sound, for the reason above: the links go to the owner. That is strictly better than before (both harms were unbounded) and it is the same trade `subscribe` has always made, but it is a real cost and the right answer to it is per-address limits that distinguish a request the owner made, which needs something the owner holds and is what this service deliberately does not have. Third time this file records the same lesson: the comment asserting parity sat two lines from the code that lacked it, and a claim about what a check refuses belongs next to a test |
| **A control character in the endpoint** | M7 | *Found by review, 2026-09-28.* `check_endpoint` looked at the scheme, the userinfo, the port and the host, and at nothing else in the string. An endpoint containing a newline passed it, passed `subscribe`, and was **committed** - and then `OutboundMessage.__post_init__` raised on the CR/LF while the confirmation message was being built, outside `deliver`'s try, so the request 500'd. What it left behind is the part that matters: a row holding somebody's coordinates that can never be confirmed (no message can be built for it), is excluded from `due_for_liveness` (which requires `confirmed_at`), and is therefore deleted by nothing, ever. A retention failure reachable by one unauthenticated POST. Three layers disagreed about control characters - `urlparse` accepts all of them, `OutboundMessage` rejects CR and LF, httpx raises `InvalidURL` on NUL and TAB - so the fix is one gate in the function whose job is deciding whether we will talk to a URL at all: printable ASCII only. `deliver` also takes a callable now, so a build that raises is inside the try on every path where the message is built from request data |
| **`InvalidURL` is not an `HTTPError`** | M7 | *Found by review, 2026-09-28.* `WebPushNotifier.send` is documented and used as "never raises, returns a `DeliveryResult`" - `RoutingNotifier` passes its value straight through and `deliver_queued` branches on it - and it caught `httpx.HTTPError`. `httpx.InvalidURL` descends from `Exception`, not from `HTTPError`, so it escaped. Every caller happens to have a broad `except Exception`, which is why nothing crashed; that is luck, not design, and one refactor from a crashed delivery run. Now caught alongside |
| **One TTL for every kind of push** | M7 | *Found by review, 2026-09-28.* `webpush_ttl_seconds` (30 min, matched to `dispatcher.MAX_NOTIFICATION_AGE`) was sent on every message. Right for a warning, wrong for everything else: a confirmation is valid for `confirm_token_ttl_hours` (24 h) and was being discarded by the push service after 30 minutes, so a reader who signed up and then spent an hour underground got nothing, was told by the page that their notifications must be misconfigured, and held a perfectly good token nobody could deliver. `OutboundMessage` now carries optional `ttl_seconds` and `urgency`; the confirmation gets its token's lifetime, the settings link its link's, and the liveness ping a day at `Urgency: normal` - it is the one message here that does not deserve to wake a sleeping phone |
| **The service worker had no behavioural tests at all** | M7 | *Raised by review, 2026-09-28, as a finding about the tests rather than the code - and it was the right call.* Seven tests named `sw.js` and every one of them asserted that a substring appeared in the file. They passed while `actions` was computed and never passed to `showNotification` (so no button was ever drawn, while `mail.py` had already dropped the unsubscribe URL from push bodies *because* the button existed), while `Notification.maxActions \|\| 2` asked a platform reporting 0 for two, and while the tab-reuse branch opened a new window for every warning after the first. `assert "maxActions" in source` passed against `slice(0, 99)` and against slicing the wrong array. `tests/js/sw_harness.mjs` now builds a worker global and `tests/js/sw_test.mjs` drives the listeners - 17 cases, bridged into pytest by `tests/test_service_worker.py`, each of the three bugs above caught by the case named for it. `tests/js/page_test.mjs` does the same for the signup page's own JavaScript. The general lesson, which is the third time this file has had to record it: for this project's browser glue, a grep is not a test, and the bugs that reach readers live exactly where no test executes |
| **A validator looser than the column it writes to** | M7 | *Found by review, 2026-09-27.* `SubscribeRequest` capped `p256dh` at 256 characters and `auth` at 128, while `subscribers.push_p256dh` and `push_auth` are `String(128)` and `String(64)`. Anything in the gap passed validation and failed on the flush as `psycopg.errors.StringDataRightTruncation` - an unhandled `DataError`, so a 500 from a public unauthenticated endpoint that anyone could trigger by posting a long string, and a rolled-back transaction rather than a rejected field. The widths are now the single constants `PUSH_P256DH_MAX_LENGTH` and `PUSH_AUTH_MAX_LENGTH` in `db/models.py`, used by both the column and the `Field`, and `test_push_key_bounds_match_the_columns` fails if the two ever separate again. The general lesson, which is why this has a row rather than just a commit: "loose bounds are lenient" is only true at the edge of the system. One layer in, a validator that accepts more than the next layer stores is not leniency, it is a validator handing the database input it cannot hold |
| **A subscription reused under a stale VAPID key** | M7 | *Found by review, 2026-09-27.* `index.html` reused any subscription `getSubscription()` returned. A push subscription is bound to the `applicationServerKey` it was created with, and a push signed with a different key is refused 403 by the push service - so a browser holding a subscription from an earlier key would sign up successfully, be stored as confirmed, and never receive a warning, with nothing on either side saying so. The page now compares `options.applicationServerKey` byte-wise against the current key and calls `unsubscribe()` on a mismatch before subscribing again. It reuses when the browser does not populate `options`, because there the alternative is discarding a subscription that probably works. This is the second defect in this file that no Python test could see - see the `PLAY is not defined` note under D-45 - and it is why the guard is asserted against the served HTML and exercised by driving the function itself |
| ~~**Leaflet is loaded from a CDN**~~ | M6 | **Resolved 2026-09-27.** Leaflet 1.9.4 is vendored under `rainalert/api/static/vendor/leaflet`, byte-identical to the npm tarball (which is what unpkg serves) and checked against the registry's own sha512 at vendoring time. `MAP_SCRIPT_SRC` is gone; `script-src` and `style-src` are `'self'` again, and no page loads a script or stylesheet from another origin. The blocker was never the work but the network — neither this environment nor the dev VM could reach unpkg — and it turned out `registry.npmjs.org` was reachable all along, which is the better source anyway because it ships a checksum to verify against. `make vendor-leaflet` re-vendors and refuses to write anything on a checksum mismatch; `tests/test_vendored_leaflet.py` pins both files' sha256 so a local edit fails the suite instead of silently becoming a fork |

## 19. Open questions

| # | Question | Needed by |
|---|---|---|
| Q-1 | Domain name and sending domain (needed for links, `User-Agent` contact, SPF/DKIM/DMARC) | M3 |
| Q-2 | Confirm the private-audience assumption (D-18). Going public adds Impressum, Datenschutzerklärung, provider DPA | before any public link |
| ~~Q-3~~ | **Resolved 2026-09-16: Cloud SQL `db-f1-micro` in `europe-west3` for production, Neon or local Postgres for dev/CI.** Reasoning in §6.3 — the 5-minute cadence exhausts Neon's free CU-hour allowance around day 16 of each month, and Neon would add a second, US-headquartered processor for the table holding email plus home coordinates | done |
| Q-4 | Mail provider account: Brevo vs Mailgun vs SendGrid (all have a usable free tier) | M3 |
| ~~Q-5~~ | **Resolved 2026-09-27: basemap.de Web Raster (BKG) is the default.** CC BY 4.0, no API key, no account, no quota, and — the property that decided it — no non-commercial clause, so it survives this service ever carrying ads. Germany only, which matches the DE1200 composite's own footprint; expanding past Germany is a radar-data problem (OPERA or per-country services) long before it is a basemap problem, so global coverage was not worth paying for. Reasoning and the alternatives in §11.1 (*Basemap tiles*) and docs/LOCAL.md §"Choosing a basemap" | done |
| Q-14 | **Does the whole push flow work on a real Android phone, and on an iPhone with the site on the Home Screen?** Everything here is asserted against a mock transport and a simulated browser: the encryption round-trips, the payload and the service worker agree on their field names, and Chromium renders the pages. What none of that covers is a real push service actually delivering - permission prompt, service worker activation, a notification in the shade, the two action buttons drawing, and `pushsubscriptionchange` firing on a rotation. On iOS the extra step is the Home Screen install, which the page describes but nobody here has performed | before telling anyone it works |
| Q-6 | Should raw archives be kept longer than 48 h — and become a permanent cold archive? They are the system of record (D-23), N-independent at ~500 GB/year, ≈ €2–4/month on Coldline, and the only thing that allows retroactively re-tuning thresholds against real weather. My recommendation: 48 h hot now, revisit once alerting is tuned | M2 |
| Q-8 | Is 12 h the right past span, or would 24 h be more useful? Storage is negligible (~22 MB per 12 h); the real limits are DWD's own file retention and slider usability | M5 |
| Q-9 | Accept the mail provider's DPA and Google's CDPA before the first friend subscribes (F-12). Ten minutes of clicking, and Art. 28 GDPR applies from the first address handed over — this is not launch paperwork | M3 |
| Q-7 | Reverse geocoding for a friendly place name in the subject line — worth an extra dependency/service? | M4 |
| ~~Q-11~~ | **Moot since D-45 (2026-09-27): there is no `ntfy://` link and no app to install.** What replaces it is narrower and still open as Q-14 | done |
| ~~Q-12~~ | **Moot since D-45.** ntfy's `Actions` header is gone; the equivalent question - whether a notification action button renders and fires - is answered by the Notification API rather than by one app's docs, and `maxActions` reports what a browser will draw | done |
| Q-13 | **Does email need RFC 8058 one-click unsubscribe, and on what terms?** It was removed rather than fixed (D-33) because it never worked; nothing sends bulk mail, and Gmail's and Yahoo's requirement starts at 5 000 messages a day. Bringing it back means two things together: a handler that reads the token from the query on `POST`, and a decision about what that exposes - deletion reachable by mail-client automation, which is F-4's worry one level up, against a token in a query string, which is D-26's. Neither is answerable before there is a sending domain | when email becomes a real channel, with Q-1 |
| **Q-10** | **`PUBLIC_BASE_URL` must move to `https://` before anyone but the author subscribes.** It is deliberately `http://<the VM's IP>:8000` during development, which needs no code - every link in every message is built from it (§12). Two things are broken while it stays that way. The session cookie drops its `Secure` flag, by design, because a `Secure` cookie over http is silently discarded and login would appear broken - so the settings session travels in clear and anyone on the path can take it. And browsers refuse geolocation outside a secure context, so the locate control on both maps cannot work at all (§11.3). The tokens themselves no longer travel in clear: confirm, unsubscribe, the magic link and a warning's location reference all ride in the fragment (D-26), which the browser never sends. **This does not need Q-1.** Cloud Run serves https on its own generated hostname with a managed certificate, so deploying there and setting this to the `api_url` output resolves it; a domain only changes what the hostname reads like | before the first friend |

---

## 20. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| DWD changes the product/format again (as with `fx` → `rv`) | Service silently stops or mis-decodes | Golden fixture test (§16.1); cycle-age alert (§15); format details read from the header, not hard-coded |
| Georeferencing off by a few cells | Warnings for the wrong place, undetectable by eye | Cross-check against wradlib at fixed reference points (§16.2) |
| Nowcast skill decays with lead time | False alarms | 30 min default (D-13); verification job measures the real hit/false-alarm ratio; thresholds are per-subscription |
| Radar outage read as "dry" | Missed warnings, wrong state transitions | `MISSING_FRACTION_LIMIT` gate (§9 step 0) |
| Mail lands in spam | Service is useless | SPF/DKIM/DMARC as an M6 gate; transactional provider; RFC 8058 unsubscribe |
| Cloud Run job overruns the 5-minute budget as subscribers grow | Cycles skipped | `rainalert_pipeline_seconds` alert; split the overlay renderer out first (§6) |
| A poisoned or corrupt upstream response suppresses everyone's alerts while monitoring stays green | Nobody is warned, and we do not find out | Plausibility gate and cycle status metric (§4.3.1, §15); staleness alone does not cover "fetched, parsed, meaningless" |
| One unevaluatable subscription aborts every cycle | Permanent outage for all users from one bad row | Per-subscription fault isolation (§8 step 4); `OutsideGrid` covers every rejected coordinate |
| Hammering DWD through a retry bug | Blocked by DWD, reputational | Attempt caps, backoff, circuit breaker, daily byte budget, idempotency (§4.3) |
| Personal data leak (email + precise location) | GDPR incident | Minimisation, hashed tokens, rounded logs, hard delete (§13) |
| 168 timeline frames overwhelm a phone on mobile data | Map page unusable where it matters most | Windowed lazy loading, no full preload, LRU eviction (§11.1) |
| Per-subscriber time series outgrows the radar archive itself | DB cost scales with N × cycles for data that is ~99 % "nothing happened" | `evaluations` is a 48 h debug log; the permanent record is event-shaped (§8.1, D-23) |
| DWD retains too little history for backfill | Timeline starts empty and fills over 12 h | M0 verifies actual retention; re-render from raw archives; purely cosmetic — alerting is unaffected |

---

## 21. References

- DWD Open Data portal — https://www.dwd.de/DE/leistungen/opendata/opendata.html
- DWD radar open data root — https://opendata.dwd.de/weather/radar/
- RV composite directory — https://opendata.dwd.de/weather/radar/composite/rv/
- DWD RADVOR product page — https://www.dwd.de/EN/ourservices/radvor/radvor.html
- DWD radar products overview — https://www.dwd.de/EN/ourservices/radar_products/radar_products.html
- wradlib RADOLAN guide — https://docs.wradlib.org/projects/radolan/en/latest/
- `wradlib.io.read_radolan_composite` — https://docs.wradlib.org/en/2.3.0/generated/wradlib.io.radolan.read_radolan_composite.html
- wradlib issue #448 (DE1200 1200×1100 support) — https://github.com/wradlib/wradlib/issues/448
- Go RADOLAN/RADVOR parser (useful second opinion on the binary format) — https://github.com/jonnyschaefer/radolan
- Home Assistant DWD precipitation integration (prior art: RV usage, late-file handling) — https://github.com/evgparen/ha-dwd-precipitation
- DWD `content.log` tooling — https://github.com/DeutscherWetterdienst/opendata-content.log-tool
- RFC 8058 (one-click unsubscribe) — https://www.rfc-editor.org/rfc/rfc8058
