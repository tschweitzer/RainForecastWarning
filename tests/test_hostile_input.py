"""The upstream archive is untrusted input.

It arrives over the internet from a third party. A compromised mirror, a hijacked route, or simply
a corrupt publication all look identical here, and ``..._LATEST`` re-serves the same bytes every
cycle - so a decoder that dies on one bad file stops alerting indefinitely, not once.

Findings F-1 and F-2 of docs/SECURITY_REVIEW.md.
"""

import bz2
import tarfile

import pytest

from rainalert.radar.decoder import (
    MAX_MEMBER_BYTES,
    MAX_MEMBERS,
    RVArchiveRejected,
    RVFormatError,
    decode_frame,
    read_frames,
)

HEADER = (
    b"RV161355100000926BY   2640195VS 5SW  P42001HPR E-02INT   5"
    b"GP1200x1100VV 000MF 00000008MS 13<deboo,dedrs>\x03"
)


def _archive(members: list[tuple[str, int]], payload: bytes = b"\x00") -> bytes:
    """A bz2 tar whose members *declare* the given sizes."""
    comp = bz2.BZ2Compressor(9)
    out = bytearray()
    for name, size in members:
        info = tarfile.TarInfo(name)
        info.size = size
        out += comp.compress(info.tobuf())
        written = 0
        block = payload * 65536
        while written < size:
            chunk = block[: min(len(block), size - written)]
            out += comp.compress(chunk)
            written += len(chunk)
        if size % 512:
            out += comp.compress(b"\x00" * (512 - size % 512))
    out += comp.compress(b"\x00" * 1024)
    out += comp.flush()
    return bytes(out)


def test_decompression_bomb_is_rejected_without_being_read(tmp_path):
    """483 bytes on the wire, 512 MiB declared. Must be refused, not decoded."""
    bomb = tmp_path / "bomb.tar.bz2"
    bomb.write_bytes(_archive([("DE1200_RV2609161355_000", 512 * 1024 * 1024)]))
    assert bomb.stat().st_size < 4096  # tiny on the wire, by construction

    with pytest.raises(RVArchiveRejected, match="limit is"):
        read_frames(bomb)


def test_too_many_members_is_rejected(tmp_path):
    path = tmp_path / "many.tar.bz2"
    path.write_bytes(_archive([(f"m{i:03d}", 16) for i in range(MAX_MEMBERS + 1)]))
    with pytest.raises(RVArchiveRejected, match="members"):
        read_frames(path)


def test_many_small_members_cannot_add_up_past_the_total(tmp_path):
    """Each member under the per-member cap, the sum far over the archive cap."""
    size = MAX_MEMBER_BYTES - 1
    path = tmp_path / "sum.tar.bz2"
    path.write_bytes(_archive([(f"m{i:03d}", size) for i in range(MAX_MEMBERS - 1)]))
    with pytest.raises(RVArchiveRejected, match="total"):
        read_frames(path)


@pytest.mark.parametrize(
    ("field", "replacement", "match"),
    [
        (b"PR E-02", b"PR E+20", "precision"),
        (b"INT   5", b"INT  60", "interval"),
        (b"GP1200x1100", b"GP0002x0001", "grid"),
        (b"VV 000", b"VV 999", "lead"),
        (b"VV 000", b"VV 007", "lead"),
    ],
)
def test_forged_header_fields_are_rejected(field, replacement, match):
    """Reading dimensions and scale from the header must not mean trusting them.

    'PR E+20' otherwise decodes to precision 1e+20 and every cell reads as catastrophic rain.
    """
    header = HEADER.replace(field, replacement)
    assert header != HEADER
    with pytest.raises(RVFormatError, match=match):
        decode_frame(header + b"\x00" * 4)


@pytest.mark.parametrize(
    ("content", "label"),
    [
        (b"not an archive at all", "garbage"),
        (b"<html><body>502 Bad Gateway</body></html>", "an error page served as an archive"),
        (b"", "an empty file"),
    ],
)
def test_damaged_archives_raise_the_documented_type(tmp_path, content, label):
    """tarfile.ReadError is not an OSError, so a caller catching RVFormatError would still die."""
    path = tmp_path / "bad.tar.bz2"
    path.write_bytes(content)
    with pytest.raises(RVFormatError):
        read_frames(path)


def test_member_shorter_than_declared_is_rejected(tmp_path):
    path = tmp_path / "short.tar.bz2"
    path.write_bytes(_archive([("DE1200_RV2609161355_000", 2640195)]))
    # Truncate the compressed stream: the member cannot deliver what it declared.
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(RVFormatError):
        read_frames(path)


def test_good_fixture_still_decodes(wet_cycle):
    """The limits must not reject the real thing."""
    assert len(read_frames(wet_cycle)) == 3
