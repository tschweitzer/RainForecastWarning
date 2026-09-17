# DWD RV product — observed facts (M0)

Working notes for milestone M0 (§18 of DESIGN.md). Everything here is **observed**, not inferred from
documentation. Source: directory listing of
`https://opendata.dwd.de/weather/radar/composite/rv/` captured 2026-09-16 ~09:28 UTC.

Status: **complete**. Sections 1–5 come from the directory listing; sections 6–10 from decoding a real
archive (`DE1200_RV_LATEST.tar.bz2`, cycle 2026-09-16 13:55 UTC, 1 814 012 B), cross-checked against
`wradlib` 2.9.6.

## 1. Two parallel products in the same directory

| | pattern | example | size range observed |
|---|---|---|---|
| A | `DE1200_RV<YYMMDDHHMM>.tar.bz2` | `DE1200_RV2609160920.tar.bz2` | 104 KB – 1.35 MB |
| B | `composite_rv_<YYYYMMDD>_<HHMM>.tar` | `composite_rv_20260916_0920.tar` | 748 KB – 5.40 MB |

Same 5-minute cadence, same retention window, published within seconds of each other. **Use A**: it is
consistently ~4× smaller for what appears to be the same cycle, which matters for the politeness
budget (§4.3).

Note on B: its sizes are exact multiples of 10240 (tar blocking factor 20 × 512 B) *and* vary with the
weather. A tar of fixed-size raw grids would be constant-size, so B's members must already be
compressed — i.e. A and B are probably not the same bytes in different wrappers. Settle with a real
file before assuming anything.

## 2. `_LATEST` aliases exist for both

```
DE1200_RV_LATEST.tar.bz2    16-Sep-2026 09:23:30    1327867
DE1200_RV2609160920.tar.bz2 16-Sep-2026 09:23:30    1327867   <- newest timestamped file
```

Identical size **and** mtime as the newest timestamped file. Consequences for the ingest client:

- Polling `DE1200_RV_LATEST.tar.bz2` with `If-Modified-Since` / `If-None-Match` is sound — the mtime
  tracks the real file, so a `304` genuinely means "no new cycle yet".
- `_LATEST` carries no timestamp in its name, so the **nominal time must come from the file header**,
  not the URL. The `radar_cycles.nominal_time` uniqueness check (§4.3 rule 8) depends on this.

## 3. Retention: ~48 hours

Oldest entry `…2609140925` (14-Sep 09:25), newest `…2609160920` (16-Sep 09:20) → a rolling window of
**47 h 55 min**, ≈ 576 cycles per product.

**This resolves the M0 backfill question and Q-8.** A 12 h timeline (D-22) can be fully backfilled at
deploy time, and 24 h would also be within reach. Our own 48 h GCS raw retention (D-7) happens to
mirror DWD's — there is no window in which DWD has a cycle we could still fetch but we have discarded.

## 4. Publication delay: ~3–5 minutes after nominal time

Nominal time in the filename vs. the server's mtime, sampled across the window:

| nominal | published | delay |
|---|---|---|
| 14-Sep 09:25 | 09:28:19 | +3 m 19 s |
| 15-Sep 13:30 | 13:33:49 | +3 m 49 s |
| 15-Sep 02:35 | 02:39:07 | +4 m 07 s |
| 14-Sep 14:45 | 14:49:24 | +4 m 24 s |
| 15-Sep 14:45 | 14:49:51 | +4 m 51 s |
| 15-Sep 02:45 | 02:50:13 | +5 m 13 s |
| 16-Sep 09:20 | 09:23:30 | +3 m 30 s |

Typical **+3 m 10 s … +3 m 30 s**; occasional **+4 … +5 m**; worst observed **+5 m 13 s**. Filename
time and mtime are in the same timezone (a UTC/CEST mismatch would show as ~2 h).

**Design consequence — schedule changed.** §4.4 originally specified `3-58/5 * * * *`, which fires
*before* the typical publication and would burn a retry on nearly every cycle. Now **`4-59/5 * * * *`**:
the first attempt lands after the common case, and the existing backoff (20/40/80/160 s, cumulative
300 s) still covers the +5 m tail. DESIGN.md §4.4 carries the measured figures.

## 5. A real radar dropout (analysed)

Three cycles on 15-Sep looked anomalous in the listing (roughly half the size of their neighbours).
Decoding them plus 16:30 as a control shows what actually happened — and the size signal was a **red
herring**: 15-Sep was a nearly dry day nationally (peak 0.14–0.79 mm/5 min across all of Germany), so
the small files are mostly just "no rain", not "no data".

The real event is a **single radar dropping out for exactly two cycles**:

| cycle | no-data @ t+0 | sites in `MS` | national max |
|---|---|---|---|
| 16:15 | 47.10 % | 17 | 0.60 mm/5min |
| 16:20 | **52.89 %** | 15 | 0.14 |
| 16:25 | **52.89 %** | 15 | 0.14 |
| 16:30 | 47.01 % | 15 | 0.79 |

The extra 76 514 no-data cells (5.80 % of the grid) at 16:20/16:25 are centred on **53.89 N, 7.09 E** —
the North Sea, exactly where **Borkum (`deasb`)** sits. Borkum's own cell is no-data at 16:20 and
16:25, valid at 16:15 and 16:30. By 16:30 the mask is back to within 790 cells of 16:15.

### The `MS` site list is not a data-quality signal

`deasb` and `deboo` are absent from the `MS` list at 16:20, 16:25 **and 16:30** — yet coverage is fully
restored at 16:30. The header's site list therefore lags reality and cannot be used to decide whether
a region has data. **Derive quality from the no-data mask, never from `MS`.**

### Why this fixture matters: it breaks the frame-0-only gate

Sampling a 2 km mask at Borkum (53.58 N, 6.66 E), with Hamburg as a control:

| cycle | `missing_fraction[0]` | frames fully missing (of 25) |
|---|---|---|
| 16:15 | **0.00** | **24** |
| 16:20 | 1.00 | 25 |
| 16:25 | 1.00 | 25 |
| 16:30 | 0.00 | 0 |

Hamburg is 0.00 / 0 throughout.

At **16:15 the analysis frame is perfectly fine while 24 of 25 forecast frames are already gone** — the
dropout appears in the nowcast before it appears in the analysis. A gate that checks only frame 0
(as DESIGN.md §8 originally specified) passes this cycle and then reads 24 empty frames as "no rain",
concluding *dry* for a location it has no data for — which can clear a `WARNED` state or suppress a
warning outright. The per-frame gate (§9 of DESIGN.md) catches it. This is real evidence for that
correction, not a hypothetical.

## 6. Archive contents

25 members, one per forecast step, all present and all exactly the same size:

```
DE1200_RV2609161355_000   2640195 B   radaradm/feze   2026-09-16 13:57
DE1200_RV2609161355_005   2640195 B
...
DE1200_RV2609161355_120   2640195 B
```

`2640195 = 1200 × 1100 × 2 + 195`. Members are **raw RADOLAN binaries** — a 195-byte ASCII header
terminated by `ETX` (`0x03`), then `1200 × 1100` little-endian `uint16`, row-major. No per-member
compression; the bz2 wrapper does all of it (1.81 MB for 66 MB of grids on a rainy cycle).

Member name = `DE1200_RV<YYMMDDHHMM>_<lead:03d>`, but see §7: the lead is in the header too, so
filename parsing is never required.

## 7. Header, verbatim

```
RV161355100000926BY   2640195VS 5SW  P42001HPR E-02INT   5GP1200x1100VV 000MF 00000008MS103<deasb,deboo,dedrs,deeis,deess,defbg,defld,dehnr,deisn,demem,deneu,denhb,deoft,depro,deros,detur,deumd>
```

| field | value | meaning |
|---|---|---|
| — | `RV` | product |
| — | `161355` | day 16, 13:55 |
| — | `10000` | radar id (national composite) |
| — | `0926` | month 09, year 26 |
| `BY` | `2640195` | total file size — matches exactly |
| `VS` | `5` | format version |
| `SW` | `P42001H` | producing software version |
| `PR` | `E-02` | **precision 0.01** — raw × 0.01 = mm per interval |
| `INT` | `5` | interval, minutes |
| `GP` | `1200x1100` | rows × columns |
| `VV` | `000` | **forecast lead in minutes** (`005`, `010`, … `120` in the other members) |
| `MF` | `00000008` | module flags |
| `MS` | `103<…>` | 17 contributing radar sites, 103 chars |

### The header time is UTC — confirmed externally

`RV161355` is **13:55 UTC**, not local time. This matters more than it looks: Germany runs UTC+2 in
summer, so reading it as local would put every warning two hours out and every map frame two hours
stale, silently and plausibly.

Two independent pieces of evidence:

1. The publication delay measured in §4 is ~3 minutes between the filename time and the server's
   mtime. That proves the two share a timezone — it does **not** say which one.
2. Cross-checked against DWD's own radar app (2026-09-17): the cycle whose header reads `161355`
   is the image DWD displays as **15:55 CEST**. 13:55 UTC + 2 h = 15:55 CEST. Confirmed.

The code tags the decoded time `UTC` and every user-facing surface converts explicitly — the alert
mail and `probe` to the subscriber's timezone, the map page to the browser's, the timeline manifest
to ISO 8601 with an offset. Nothing displays a bare unlabelled time, which is what made this worth
pinning down rather than assuming.

Everything the decoder needs — dimensions, precision, interval, lead, nominal time — is in the header.
Nothing must be hard-coded and nothing must be parsed from the filename. This confirms the approach in
DESIGN.md §5.

## 8. Values and the no-data sentinel

- Valid values: raw `0 … 1180` on this cycle → **0.00 … 11.80 mm / 5 min** after `PR`.
- **No data is the sentinel `0x29C4` (10692)**, covering **46.7 %** of the grid at t+0. That is the
  area outside radar coverage — the DE1200 rectangle is much larger than the radar network's reach.
  It is *not* an outage.
- `cluttermask` and `secondary` were both **empty** in this file, and bits `0x1000`, `0x4000`,
  `0x8000` were unset in every cell. Those code paths therefore cannot be exercised by this fixture.

### The trap that would have shipped

The obvious-looking "strip the flag bits" decode is catastrophically wrong here:

```
0x29C4 & 0x0FFF = 2500   ->   2500 × 0.01 = 25.00 mm/5min
```

Masking value bits turns every no-data cell into a plausible **extreme-rain reading** — 25 mm in five
minutes — across 47 % of the grid. It would not crash, would not look obviously wrong in a histogram,
and would alert every subscriber permanently. **Compare against the sentinel first; never mask bits
blind.** This is a required test case.

## 9. The no-data region moves with lead time

It does not merely shrink — it advects with the forecast field:

| | t+0 | t+120 |
|---|---|---|
| no-data share | 46.73 % | 48.48 % |

Between t+0 and t+120: **102 615** cells go valid → no-data, and **79 483** go no-data → valid. The
no-data mask is therefore **per frame**, and not nested.

**Design consequence:** a subscriber near the edge of coverage can have good data at t+0 and none at
t+45. Computing `missing_fraction` once from frame 0 (as DESIGN.md §8 originally specified) would
silently evaluate garbage for those locations. It must be computed per frame.

## 10. Cross-check against wradlib 2.9.6

```python
data, meta = wradlib.io.read_radolan_composite('DE1200_RV2609161355_000')
```

- `meta` reports exactly the header fields above: `producttype='RV'`, `precision=0.01`,
  `intervalseconds=300`, `nrow=1200`, `ncol=1100`, `formatversion=5`, `predictiontime=0`,
  `datetime=2026-09-16 13:55`, `radolanversion='P42001H'`, `maxrange='100 km'`.
- A 20-line decode (`uint16` → mask `0x29C4` → × 0.01) is **bit-identical** to wradlib: values equal
  to zero tolerance on all valid cells, and the no-data mask matches on all 616 828 cells.
- Note for the golden test: wradlib returns `-9999.0` for no-data, **not** `NaN`, and exposes
  `meta['nodatamask']` as flat indices. Compare against those, not against `np.isnan`.

This confirms D-21: the own-decoder plan is sound, and wradlib is a good oracle rather than a needed
runtime dependency.

## 11. Georeferencing

Projection, exactly as DWD defines it — a sphere, not an ellipsoid:

```
+proj=stere +lat_0=90 +lat_ts=60 +lon_0=10 +x_0=0 +y_0=0 +R=6370040 +units=km +no_defs
```

DE1200 ties to the reference point 9.0 E / 51.0 N at grid offsets `j_0 = 470`, `i_0 = 600`, with
1 km cells. `rainalert/radar/grid.py` pins these constants and the test suite compares the resulting
lon/lat against `wradlib.georef.get_radolan_grid(1200, 1100, wgs84=True)` over **all 1 320 000
cells** (max deviation < 1e-9°), not at a handful of points — a systematic offset of a few cells is
invisible everywhere else and warns the wrong village.

Two conventions that are easy to get wrong:

- **Row 0 is the southern edge.** Images must be flipped vertically when rendering north-up (§11.1
  of DESIGN.md).
- **`get_radolan_grid` returns each cell's lower-left corner, not its centre** (its default mode is
  `radolan`; `mode="center"` gives centres). Anything distance-related must add half a cell.
  *An earlier revision of this document called those values "cell centres" — they are corners.*

Spot check, using cell centres, against the decoded field:

| | row | col | cell centre | value |
|---|---|---|---|---|
| Frankfurt 50.1109 N 8.6821 E | 496 | 444 | 50.1116, 8.6853 | 0.00 mm/5min |
| Hamburg 53.5511 N 9.9937 E | 894 | 543 | 53.5477, 10.0006 | 0.00 mm/5min |
| Munich 48.1351 N 11.5820 E | 263 | 668 | 48.1345, 11.5758 | 0.23 mm/5min |

Centres land within half a cell of the true coordinates, as they must.

### Radius masks use ground distance

The projection's scale factor at German latitudes is ≈ 1.09 (the standard parallel is 60 N), so a
"1 km" grid cell is about 0.92 km on the ground and projected kilometres are ~9 % longer than real
ones. Selecting a subscriber's radius in projected units would quietly inflate every radius by that
much, so `radius_mask` measures true geodesic distance (WGS84) from the point to each cell centre.

## 12. Fixtures

| file | size | contents |
|---|---|---|
| `DE1200_RV2609161355_trimmed.tar.bz2` | 214 KB | frames `_000`, `_060`, `_120` of the 2026-09-16 13:55 cycle — a wet cycle. Three frames so the moving no-data mask (§9) is testable. |
| `DE1200_RV_outage_20260915_1615-1630.tar.bz2` | 81 KB | frames `_000`, `_005`, `_060` of each of the four cycles in §5 — the Borkum dropout plus its control. |

Tests must not assert "25 members" against either fixture; assert member naming and header parsing.

The outage fixture pins three behaviours: the per-frame missing gate (16:15, where t+0 is clean and
the forecast is not), full suppression at 16:20/16:25, and recovery at 16:30 — with Hamburg as an
unaffected control in the same files.

Still unrepresented: a cycle with clutter or secondary flags set. No deadline on that one — any
archive from a day with ground clutter would do.
