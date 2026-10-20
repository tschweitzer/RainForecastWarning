"""DWD's weather radars, for saying which one is down (DESIGN.md D-75).

Positions as each radar states them in its own files (`where/lat`, `where/lon` of the
`sweep_pcp_z` product under opendata.dwd.de/weather/radar/sites/, read 2026-10-20), with the WMO
number from `what/source`. Names are the places the sites are named after, as DWD names them.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How far a radar's data reaches into the composite. DWD builds RADOLAN from each site's
#: precipitation scan out to 150 km; past that a missing radar cannot be what is missing here.
RANGE_M = 150_000


@dataclass(frozen=True)
class Site:
    code: str
    name: str
    wmo: int
    lat: float
    lon: float


SITES: dict[str, Site] = {
    site.code: site
    for site in (
        Site("deasb", "Borkum", 10103, 53.56413, 6.74832),
        Site("deboo", "Boostedt", 10132, 54.00438, 10.04690),
        Site("dedrs", "Dresden", 10488, 51.12464, 13.76864),
        Site("deeis", "Eisberg", 10780, 49.54067, 12.40279),
        Site("deess", "Essen", 10410, 51.40565, 6.96711),
        Site("defbg", "Feldberg", 10908, 47.87361, 8.00361),
        Site("defld", "Flechtdorf", 10440, 51.31120, 8.80200),
        Site("dehnr", "Hannover", 10339, 52.46008, 9.69453),
        Site("deisn", "Isen", 10873, 48.17471, 12.10178),
        Site("demem", "Memmingen", 10950, 48.04214, 10.21922),
        Site("deneu", "Neuhaus", 10557, 50.50011, 11.13503),
        Site("denhb", "Neuheilenbach", 10605, 50.10966, 6.54833),
        Site("deoft", "Offenthal", 10629, 49.98474, 8.71293),
        Site("depro", "Prötzel", 10392, 52.64867, 13.85821),
        Site("deros", "Rostock", 10169, 54.17566, 12.05808),
        Site("detur", "Türkheim", 10832, 48.58538, 9.78267),
        Site("deumd", "Ummendorf", 10356, 52.16010, 11.17609),
    )
}


def responsible(lat: float, lon: float, missing: list[str] | tuple[str, ...]) -> Site | None:
    """The nearest of the ``missing`` radars that covers this point, or None.

    None is the honest answer more often than it looks: the composite overlaps, so a radar being
    down does not always leave a hole, and a hole is not always a radar being down (beam blockage,
    clutter filtering, the edge of the network). The caller then says that data is missing, not why.
    """
    from rainalert.radar.grid import _GEOD

    best: tuple[float, Site] | None = None
    for code in missing:
        site = SITES.get(code)
        if site is None:
            continue
        _, _, distance = _GEOD.inv(lon, lat, site.lon, site.lat)
        if distance <= RANGE_M and (best is None or distance < best[0]):
            best = (distance, site)
    return best[1] if best else None
