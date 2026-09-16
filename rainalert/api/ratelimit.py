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
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from rainalert.db.models import RateLimitHit


def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    if trusted_proxy_hops <= 0:
        return request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if len(parts) >= trusted_proxy_hops:
        return parts[-trusted_proxy_hops]
    return request.client.host if request.client else "unknown"


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
    now = now or datetime.now(UTC)
    result = session.execute(
        delete(RateLimitHit).where(RateLimitHit.occurred_at < now - timedelta(days=retention_days))
    )
    session.commit()
    return result.rowcount or 0
