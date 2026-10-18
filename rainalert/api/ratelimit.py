"""Rate limiting, and the question of who the client actually is.

``X-Forwarded-For`` is a list that each proxy *appends* to, so the leftmost entries are whatever the
client sent - including entries the client invented. Only the rightmost ``trusted_proxy_hops``
entries were added by infrastructure we control. Taking the leftmost value, which is the common
shortcut, lets any client pick its own identity and bypass every limit here
(SECURITY_REVIEW.md F-5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from rainalert.db.models import RateLimitHit
from rainalert.tokens import hash_ip

#: A bucket ending in what `ip_bucket` writes. Anything else under an IP prefix is from before it.
_HASHED = r":[0-9a-f]{64}$"


def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    if trusted_proxy_hops <= 0:
        return request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if len(parts) >= trusted_proxy_hops:
        return parts[-trusted_proxy_hops]
    return request.client.host if request.client else "unknown"


def ip_bucket(action: str, ip: str, secret: str) -> str:
    """The rate-limit bucket for one client address: `<action>:<salted hash of the IP>`.

    The same HMAC as the consent record (`tokens.hash_ip`). The limits only need to know that two
    requests came from the same address, never which address it was, and these rows outlive the
    account they belong to (no cascade, see `RateLimitHit`) - so they were a log of which IP signed
    up, opened settings or tapped a warning, in plain text (DESIGN.md D-73).
    """
    return f"{action}:{hash_ip(ip, secret).hex()}"


def hit_and_check(
    session: Session, bucket: str, limit: int, window: timedelta, now: datetime | None = None
) -> bool:
    """Record an attempt and report whether it is within the limit.

    The attempt is recorded either way: the record of abuse should not depend on whether the abuse
    succeeded, and deleting an account must not erase it (hence no cascade on this table).
    """
    now = now or datetime.now(UTC)
    session.add(RateLimitHit(bucket=bucket, occurred_at=now))
    session.commit()
    count = session.execute(
        select(func.count())
        .select_from(RateLimitHit)
        .where(RateLimitHit.bucket == bucket, RateLimitHit.occurred_at >= now - window)
    ).scalar_one()
    return count <= limit


def purge_old_hits(session: Session, retention_days: int, now: datetime | None = None) -> int:
    """Delete counters past `rate_limit_retention_days` - and any row still keyed by a plain IP.

    The second half is the one-time cleanup for D-73: rows written before `ip_bucket` existed
    carry the address in clear. They would age out by themselves within a week, but the privacy
    page says hashed from the moment this ships, so they go on the first run instead. Cheap to
    keep: once they are gone it matches nothing.
    """
    now = now or datetime.now(UTC)
    plain_ip = or_(
        RateLimitHit.bucket.like("%:ip:%"), RateLimitHit.bucket.like("devicekey:fail:%")
    ) & ~RateLimitHit.bucket.regexp_match(_HASHED)
    result = session.execute(
        delete(RateLimitHit).where(
            (RateLimitHit.occurred_at < now - timedelta(days=retention_days)) | plain_ip
        )
    )
    session.commit()
    return result.rowcount or 0
