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
    MAX_TOTAL_BYTES,
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
    """483 bytes on the wire, 512 MiB declared. Must be refused, not decoded.

    The refusal now happens during decompression rather than after reading the tar headers:
    the decoder unpacks the bz2 itself, in bounded chunks, and stops at MAX_TOTAL_BYTES. That
    is strictly earlier than the old check on declared member sizes, so the bomb never reaches
    a buffer at all - but it is also why this asserts on the limit rather than on the wording
    of the member-size message.
    """
    bomb = tmp_path / "bomb.tar.bz2"
    bomb.write_bytes(_archive([("DE1200_RV2609161355_000", 512 * 1024 * 1024)]))
    assert bomb.stat().st_size < 4096  # tiny on the wire, by construction

    with pytest.raises(RVArchiveRejected, match="limit"):
        read_frames(bomb)


def test_the_bomb_never_allocates_more_than_the_limit(tmp_path):
    """The point of the bound is the memory, not the message."""
    import tracemalloc

    bomb = tmp_path / "bomb.tar.bz2"
    bomb.write_bytes(_archive([("DE1200_RV2609161355_000", 512 * 1024 * 1024)]))

    tracemalloc.start()
    try:
        with pytest.raises(RVArchiveRejected):
            read_frames(bomb)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # 512 MiB was declared. Anything near that means the bound did not hold.
    assert peak < 2 * MAX_TOTAL_BYTES, f"peaked at {peak / 1e6:.0f} MB"


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


# --- the fast path is a second way in, and needs the same guards -----------------------------


def test_the_fast_path_refuses_the_bomb_too(tmp_path):
    """read_analysis_frame unpacks less, which is not the same as trusting more."""
    from rainalert.radar.decoder import read_analysis_frame

    bomb = tmp_path / "bomb.tar.bz2"
    bomb.write_bytes(_archive([("DE1200_RV2609161355_000", 512 * 1024 * 1024)]))

    with pytest.raises(RVArchiveRejected, match="limit"):
        read_analysis_frame(bomb)


def test_the_fast_path_bomb_never_allocates_more_than_the_limit(tmp_path):
    import tracemalloc

    from rainalert.radar.decoder import read_analysis_frame

    bomb = tmp_path / "bomb.tar.bz2"
    bomb.write_bytes(_archive([("DE1200_RV2609161355_000", 512 * 1024 * 1024)]))

    tracemalloc.start()
    try:
        with pytest.raises(RVArchiveRejected):
            read_analysis_frame(bomb)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * MAX_TOTAL_BYTES, f"peaked at {peak / 1e6:.0f} MB"


@pytest.mark.parametrize(
    ("content", "label"),
    [
        (b"", "empty"),
        (b"<html>404</html>", "an error page where an archive was expected"),
        (b"BZh9" + b"\xff" * 64, "a bz2 header and then noise"),
    ],
)
def test_the_fast_path_refuses_damaged_archives(tmp_path, content, label):
    from rainalert.radar.decoder import read_analysis_frame

    path = tmp_path / "bad.tar.bz2"
    path.write_bytes(content)
    with pytest.raises(RVArchiveRejected):
        read_analysis_frame(path)


def test_the_fast_path_refuses_an_archive_truncated_mid_member(tmp_path):
    """Stopping early must not mean accepting an archive that stops early."""
    from rainalert.radar.decoder import read_analysis_frame

    whole = _archive([("DE1200_RV2609161355_000", 4096)])
    path = tmp_path / "cut.tar.bz2"
    path.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(RVArchiveRejected):
        read_analysis_frame(path)


def test_the_fast_path_refuses_an_archive_that_does_not_start_with_t0(tmp_path, wet_cycle):
    """It must say so rather than render a forecast frame as if it were the analysis."""
    import bz2
    import io
    import tarfile

    from rainalert.radar.decoder import read_analysis_frame

    raw = bz2.decompress(wet_cycle.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        members = [(m, tar.extractfile(m).read()) for m in tar.getmembers()]

    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as writer:
        for info, payload in reversed(members):  # t+120 first
            fresh = tarfile.TarInfo(name=info.name)
            fresh.size = len(payload)
            writer.addfile(fresh, io.BytesIO(payload))

    path = tmp_path / "reordered.tar.bz2"
    path.write_bytes(bz2.compress(out.getvalue()))
    with pytest.raises(RVArchiveRejected, match="analysis frame"):
        read_analysis_frame(path)
