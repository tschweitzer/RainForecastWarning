# RainForecastWarning

A cloud service that warns you by email shortly before it starts raining at your location,
using DWD radar nowcast data (Germany).

**Status:** design phase — no code yet.

Start here: **[docs/DESIGN.md](docs/DESIGN.md)** — requirements, decisions log, architecture,
data model, alert state machine, API, privacy, testing strategy and milestones.

First implementation step is milestone **M0** in that document: a short spike that verifies the
exact layout of the DWD `RV` radar archives against the live open-data server.

Data basis: Deutscher Wetterdienst (DWD), radar product RV, CC BY 4.0.
