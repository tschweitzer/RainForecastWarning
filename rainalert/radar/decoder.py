"""Decoder for DWD RADOLAN/RADVOR RV composite files.

The format is documented from observation in ``docs/DWD_RV_FORMAT.md``: a 195-byte ASCII header
terminated by ETX, followed by ``rows * cols`` little-endian uint16 values, row-major, with row 0
at the *southern* edge.

Everything needed to interpret the payload - dimensions, precision, interval, nominal time and the
forecast lead - comes out of the header. Nothing is hard-coded and nothing is taken from the
filename, so a file fetched as ``..._LATEST.tar.bz2`` is self-describing.
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

ETX = 0x03

#: Sentinel for "no measurement here". See the warning in :func:`decode_frame`.
NODATA = 0x29C4

# Limits on what an archive may contain. The upstream file is fetched over the internet from a
# third party, so it is untrusted input even though the third party is a national weather service:
# a compromised mirror, a hijacked route, or simply a corrupt publication all look the same here.
# A 483-byte bz2 archive can declare a 512 MiB member (ratio 1:1111533); decoding it unbounded
# OOMs the job, and because ..._LATEST keeps serving the same bytes it OOMs every cycle after that.
MAX_MEMBERS = 32
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024

# Accepted header ranges. Reading dimensions and precision from the header rather than hard-coding
# them is right, but unvalidated it means trusting a remote party with the allocation size and the
# scale of every reading: a forged "PR E+20" otherwise decodes to precision 1e+20.
VALID_PRECISIONS = (1e-1, 1e-2, 1e-3)
VALID_INTERVALS = (5,)
MIN_DIM, MAX_DIM = 1000, 1400
MAX_LEAD_MINUTES = 120
EXPECTED_SHAPE = (1200, 1100)

_FIELD = {
    "BY": re.compile(rb"BY\s*(\d+)"),
    "VS": re.compile(rb"VS\s*(\d+)"),
    "SW": re.compile(rb"SW\s*(\S+)"),
    "PR": re.compile(rb"PR\s*([E\-+\d.]+)"),
    "INT": re.compile(rb"INT\s*(\d+)"),
    "GP": re.compile(rb"GP\s*(\d+)x\s*(\d+)"),
    "VV": re.compile(rb"VV\s*(-?\d+)"),
    "MF": re.compile(rb"MF\s*(\d+)"),
}
_MS = re.compile(rb"MS\s*(\d+)<([^>]*)>")


logger = logging.getLogger(__name__)


class RVFormatError(ValueError):
    """The file does not look like an RV composite."""


class RVArchiveRejected(RVFormatError):
    """The archive violates a size or count limit and was not decoded at all."""


@dataclass(frozen=True)
class RVFrame:
    """One forecast step of one RV cycle."""

    values: np.ndarray  # float32 (rows, cols), mm per interval; NaN where missing
    missing: np.ndarray  # bool (rows, cols)
    nominal_time: datetime  # UTC, start of the cycle
    lead_minutes: int
    interval_minutes: int
    precision: float
    radar_sites: tuple[str, ...]
    raw_header: str

    @property
    def valid_time(self) -> datetime:
        """The time this frame describes."""
        return self.nominal_time + timedelta(minutes=self.lead_minutes)

    @property
    def shape(self) -> tuple[int, int]:
        return self.values.shape  # type: ignore[return-value]


def parse_header(blob: bytes) -> tuple[dict, int]:
    """Parse the ASCII header. Returns the fields and the offset where the payload starts."""
    end = blob.find(bytes([ETX]))
    if end < 0:
        raise RVFormatError("no ETX terminator - not a RADOLAN file")
    head = blob[:end]
    if not head.startswith(b"RV"):
        raise RVFormatError(f"expected product RV, got {head[:2]!r}")

    def need(key: str) -> re.Match:
        m = _FIELD[key].search(head)
        if m is None:
            raise RVFormatError(f"header field {key} missing")
        return m

    # A malformed date field otherwise escapes as a bare ValueError from int()/datetime(), which
    # callers catching RVFormatError would not see - the same class of bug as the grid coordinates.
    try:
        nominal_time = datetime(
            2000 + int(head[15:17]),
            int(head[13:15]),
            int(head[2:4]),
            int(head[4:6]),
            int(head[6:8]),
            tzinfo=UTC,
        )
    except ValueError as exc:
        raise RVFormatError(f"malformed timestamp in header: {exc}") from exc
    gp = need("GP")
    ms = _MS.search(head)

    precision = float(b"1" + need("PR").group(1))
    interval = int(need("INT").group(1))
    rows, cols = int(gp.group(1)), int(gp.group(2))
    lead = int(need("VV").group(1))

    if not MIN_DIM <= rows <= MAX_DIM or not MIN_DIM <= cols <= MAX_DIM:
        raise RVFormatError(f"implausible grid {rows}x{cols}")
    if precision not in VALID_PRECISIONS:
        raise RVFormatError(f"unexpected precision {precision!r}")
    if interval not in VALID_INTERVALS:
        raise RVFormatError(f"unexpected interval {interval} min")
    if not 0 <= lead <= MAX_LEAD_MINUTES or lead % 5:
        raise RVFormatError(f"implausible forecast lead {lead} min")
    if (rows, cols) != EXPECTED_SHAPE:
        # In range but not what DE1200 has always been: decode it, but this should page someone
        # rather than silently reshape the national composite.
        logger.warning("RV grid is %dx%d, expected %dx%d", rows, cols, *EXPECTED_SHAPE)

    fields = {
        "producttype": "RV",
        "radarid": head[8:13].decode("ascii"),
        "nominal_time": nominal_time,
        "datasize": int(need("BY").group(1)),
        "formatversion": int(need("VS").group(1)),
        "softwareversion": need("SW").group(1).decode("ascii"),
        "precision": precision,  # "E-02" -> 0.01
        "interval_minutes": interval,
        "rows": rows,
        "cols": cols,
        "lead_minutes": lead,
        "moduleflag": int(need("MF").group(1)),
        "radar_sites": tuple(ms.group(2).decode("ascii").split(",")) if ms else (),
        "raw_header": head.decode("latin-1"),
    }
    return fields, end + 1


def decode_frame(blob: bytes) -> RVFrame:
    """Decode one RV member.

    .. warning::
       No-data is the sentinel ``0x29C4`` and is detected by **comparing against it**, never by
       masking flag bits off the value. ``0x29C4 & 0x0FFF == 2500``, which after the precision
       factor is a plausible-looking 25.00 mm/5min - so a bit-masking decoder silently turns ~47 %
       of the grid (everything outside radar range) into extreme rain.
    """
    fields, offset = parse_header(blob)
    rows, cols = fields["rows"], fields["cols"]
    expected = rows * cols * 2
    payload = blob[offset:]
    if len(payload) != expected:
        raise RVFormatError(f"payload is {len(payload)} bytes, expected {expected}")

    raw = np.frombuffer(payload, dtype="<u2").reshape(rows, cols)
    missing = raw == NODATA
    values = np.where(missing, np.nan, raw * fields["precision"]).astype(np.float32)

    return RVFrame(
        values=values,
        missing=missing,
        nominal_time=fields["nominal_time"],
        lead_minutes=fields["lead_minutes"],
        interval_minutes=fields["interval_minutes"],
        precision=fields["precision"],
        radar_sites=fields["radar_sites"],
        raw_header=fields["raw_header"],
    )


def read_frames(archive: Path | str | bytes) -> list[RVFrame]:
    """Decode every member of an RV ``.tar.bz2``, ordered by cycle then forecast lead.

    Accepts a path or the raw bytes. The ingest job holds the archive in memory and never writes it
    to disk, which is what makes tar path traversal a non-issue here; keep it that way.

    Accepts archives holding more than one cycle - fixtures do, the real product does not.
    """
    frames: list[RVFrame] = []
    source: dict = (
        {"fileobj": io.BytesIO(archive)} if isinstance(archive, bytes) else {"name": archive}
    )
    with tarfile.open(mode="r:*", **source) as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        _check_limits(members)
        for member in members:
            handle = tar.extractfile(member)
            if handle is None:
                continue
            # Bounded read: one byte more than declared, so a member that lies about its size is
            # caught rather than streamed into memory.
            blob = handle.read(member.size + 1)
            if len(blob) != member.size:
                raise RVArchiveRejected(
                    f"member {member.name!r} declared {member.size} bytes, delivered {len(blob)}"
                )
            frames.append(decode_frame(blob))
    frames.sort(key=lambda f: (f.nominal_time, f.lead_minutes))
    return frames


def _check_limits(members: list[tarfile.TarInfo]) -> None:
    """Reject the archive as a whole before any member is read.

    Deliberately all-or-nothing: decoding "the good parts" of a suspicious archive would let an
    attacker choose which forecast steps we see.
    """
    if len(members) > MAX_MEMBERS:
        raise RVArchiveRejected(f"{len(members)} members, limit is {MAX_MEMBERS}")
    total = 0
    for member in members:
        if member.size > MAX_MEMBER_BYTES:
            raise RVArchiveRejected(
                f"member {member.name!r} declares {member.size} bytes, limit is {MAX_MEMBER_BYTES}"
            )
        total += member.size
    if total > MAX_TOTAL_BYTES:
        raise RVArchiveRejected(f"archive declares {total} bytes total, limit is {MAX_TOTAL_BYTES}")


def read_cycle(archive: Path | str | bytes) -> list[RVFrame]:
    """Decode a single RV cycle, ordered by forecast lead.

    Raises if the archive holds frames from more than one nominal time: silently merging cycles
    produces a plausible-looking list with duplicate leads, which is worse than an error.

    A complete cycle holds 25 members (t+0 ... t+120); fixtures hold fewer, so the count is not
    enforced here. Callers needing a complete cycle should check.
    """
    frames = read_frames(archive)
    stamps = {f.nominal_time for f in frames}
    if len(stamps) > 1:
        raise RVFormatError(
            f"archive holds {len(stamps)} cycles ({', '.join(sorted(s.strftime('%H:%M') for s in stamps))}); "
            "use read_frames() and group by nominal_time"
        )
    return frames
