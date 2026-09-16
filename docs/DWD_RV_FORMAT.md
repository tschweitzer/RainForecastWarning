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

## 5. A real outage to test against

Three consecutive cycles on 15-Sep are ~half the size of their neighbours while the surrounding trend
is rising:

```
composite_rv_20260915_1610.tar   1413120
composite_rv_20260915_1615.tar    890880   <-
composite_rv_20260915_1620.tar    870400   <-
composite_rv_20260915_1625.tar    880640   <-
composite_rv_20260915_1630.tar   1433600
```

(The `.tar.bz2` variant shows the same dip: 260547 → 127703 / 125251 / 129310 → 273695.)

Most likely a partial composite — one or more radars missing — rather than a quiet spell, since the
neighbouring cycles bracket it at double the size. **These three cycles are worth keeping as fixtures
for the missing-data gate (§9 step 0) and the timeline gap rendering (§11.1)**: a real partial-outage
case is much better than a synthetic one. They age out of DWD's window on 17-Sep ~16:15, so fetch them
before then if we want them.

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

`wradlib.georef.get_radolan_grid(1200, 1100, wgs84=True)` → `(1200, 1100, 2)` lon/lat.

- SW corner `grid[0,0]` = 3.5519 E, 45.6959 N; NE corner `grid[-1,-1]` = 18.7494 E, 55.8411 N.
- **Row 0 is the southern edge.** Images must be flipped vertically when rendering north-up (§11.1).

Spot check against the decoded field:

| | row | col | cell centre | value |
|---|---|---|---|---|
| Frankfurt 50.1109 N 8.6821 E | 496 | 444 | 50.1073, 8.6788 | 0.00 mm/5min |
| Hamburg 53.5511 N 9.9937 E | 895 | 543 | 53.5521, 9.9932 | 0.00 mm/5min |
| Munich 48.1351 N 11.5820 E | 264 | 669 | 48.1385, 11.5823 | 0.10 mm/5min |

Cell centres land within ~0.004° (≈ 300 m) of the true coordinates, as expected for a 1 km grid.

## 12. Fixture

`tests/fixtures/DE1200_RV2609161355_trimmed.tar.bz2` (214 KB) — members `_000`, `_060`, `_120` of the
2026-09-16 13:55 cycle. Three frames rather than two so that the moving no-data mask (§9) is testable.
Tests must not assert "25 members" against this fixture; assert member naming and header parsing.

Still unrepresented, and worth capturing separately if the code paths matter: a cycle with clutter or
secondary flags set, and the partial-outage cycles of §5.
