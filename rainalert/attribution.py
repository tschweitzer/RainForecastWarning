"""Who the data belongs to, said once.

DWD open data is CC BY 4.0. The licence wants the source named, the licence named and linked,
**and any modification indicated** - and this service modifies heavily: the RADOLAN grid is
reprojected to Web Mercator, downsampled to roughly 2 km, and turned into colours. A credit that
does not say so is an incomplete one, which is why DESIGN.md 4.2 asks for it in as many words.

One constant rather than the four copies this used to be - footer, alert messages, and the two
timeline payloads - because four strings that must agree are four strings that will not.
"""

from __future__ import annotations

#: The plain-text form, for messages and JSON.
ATTRIBUTION = (
    "Datenbasis: Deutscher Wetterdienst (DWD), Radarprodukt RV, CC BY 4.0 "
    "- eigene Verarbeitung (umprojiziert, vergroebert, eingefaerbt)"
)

#: The same thing for the web footer, where the licence can be a link and umlauts are safe.
#: The plain one above is ASCII because it was written for mail headers, which no longer exist
#: (D-76); it is kept as it is because the timeline API publishes it verbatim.
ATTRIBUTION_HTML = (
    "Datenbasis: Deutscher Wetterdienst (DWD), Radarprodukt RV, "
    '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a> '
    "&ndash; eigene Verarbeitung (umprojiziert, vergr&ouml;bert, eingef&auml;rbt)"
)
