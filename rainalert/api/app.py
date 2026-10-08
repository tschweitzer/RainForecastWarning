"""FastAPI application: the REST API and the server-rendered pages.

Shape notes that are security decisions rather than style:

* Everything subscriber-scoped is addressed as ``/me`` and resolved from the bearer token. There is
  no object id in any path, so the entire IDOR class does not exist.
* ``POST /api/v1/subscriptions`` answers identically whether or not the address is known, so it
  cannot be used to test whether someone is subscribed.
* Confirm and unsubscribe are **side-effect free on GET**. Mail scanners follow links; a
  state-changing GET means the scanner consumes the token and the real user is told it is already
  used, and on unsubscribe it would silently delete the account (SECURITY_REVIEW.md F-4).
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from rainalert import subscriptions as svc
from rainalert.api.assets import VersionedStaticFiles, static_url
from rainalert.api.mail import (
    confirmation_message,
    deletion_receipt,
    manage_link_message,
    push_keys,
)
from rainalert.api.metrics import render as render_metrics
from rainalert.api.ratelimit import client_ip, hit_and_check
from rainalert.attribution import ATTRIBUTION_HTML
from rainalert.config import Settings, get_settings
from rainalert.db.models import (
    PUSH_AUTH_MAX_LENGTH,
    PUSH_P256DH_MAX_LENGTH,
    Channel,
    Subscriber,
    Subscription,
    TokenPurpose,
)
from rainalert.db.schema import schema_complaint
from rainalert.db.session import make_engine, make_session_factory
from rainalert.notify import Notifier, build_notifier
from rainalert.notify.webpush import WebPushNotifier
from rainalert.radar.overlay import LAYER_OPACITY, legend
from rainalert.storage import GCSOverlayStore, LocalOverlayStore, OverlayStore
from rainalert.timeline import build_timeline
from rainalert.tokens import (
    csrf_token,
    hash_address,
    same_secret,
    session_token,
    verify_csrf_token,
    verify_locate_token,
    verify_manage_request_token,
    verify_session_token,
    verify_unsubscribe_token,
)

logger = logging.getLogger(__name__)

#: Upper bound for anything that identifies a subscriber in a request body - a mailbox or a push
#: endpoint. Shared so the subscribe route and the settings-link route cannot drift apart again; the
#: column behind both is `Text`, so this is a request-size bound rather than a storage one.
MAX_ADDRESS_LENGTH = 2048

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# Available to every template without threading it through every context dict. The credit
# belongs on every page, so it should not depend on each handler remembering to pass it.
TEMPLATES.env.globals["attribution_html"] = ATTRIBUTION_HTML
# Scripts and styles by content-versioned URL, so a deploy cannot be hidden by a cached copy
# (assets.py, D-57).
TEMPLATES.env.globals["static_url"] = static_url

#: The basemap styles the vector map can ask for: one for each colour scheme (D-58).
MAP_THEMES = ("gray", "gray-dark")


def map_style_template(theme: str) -> dict:
    """A fresh copy of a committed style, parsed. A copy because the caller fills it in."""
    path = Path(__file__).parent / "static" / "map" / f"{theme}.json"
    return json.loads(path.read_text(encoding="utf-8"))


#: The settings-page session.
#:
#: The name is not ours to choose. Firebase Hosting - which fronts this service, because Cloud Run
#: offers no domain mapping in europe-west3 - **strips every cookie except one named `__session`**
#: from the requests it proxies to the backend. It does that so it can cache: when the cookie is
#: present it goes into the cache key, so two visitors with different sessions cannot be served each
#: other's response.
#:
#: This was `rainalert_manage`, and the symptom of getting it wrong is not an error anywhere. The
#: magic link redeems, the session cookie is set, and then every request that needs it arrives
#: without it: `GET /api/v1/subscriptions/me` answers 401 and the settings page says "Deine
#: Einstellungen konnten gerade nicht geladen werden" - which reads as a server fault and sends the
#: reader off to request another link that will fail the same way.
#:
#: Generic as the name is, it is still host-scoped, and `web.app` is on the Public Suffix List - so
#: no other `*.web.app` site can set or read a cookie for this host. On a custom domain later the
#: same holds for that domain.
#:
#: Not prefixed `__Host-`, which would be the stronger choice, because that prefix requires Secure
#: and this service is served over plain http in development - a cookie the browser silently refuses
#: to store is a page that silently never logs in. `__session` is also exactly the name Hosting
#: looks for, so a prefix would defeat the point.
MANAGE_COOKIE = "__session"
#: Echoed back on every write from the settings page. A custom header cannot be set by a plain
#: cross-site form, so requiring one already forces a preflight; the value being unguessable is
#: what makes the preflight pointless to attempt (SECURITY_REVIEW.md F-16).
CSRF_HEADER = "X-Rain-CSRF"


def image_origin(url: str) -> str:
    """The one origin img-src should allow for a configured image source, or nothing.

    Used for both the basemap tiles and the radar overlays. Derived from the configured URL
    rather than hard-coded, so the policy can never be broader than what is actually in use -
    and is empty when nothing is configured, which is the default for tiles. A tile server sees
    every pan and zoom, so this is worth keeping narrow.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    host = parsed.netloc
    # {s}.tiles.example.com is Leaflet's subdomain placeholder; allow the siblings, not the web.
    if host.startswith("{s}."):
        return f"{parsed.scheme}://*.{host[4:]}"
    return f"{parsed.scheme}://{host}"


#: Decimal places kept for a stored coordinate. Four is about 11 m, which is already far finer
#: than anything the service can act on: it samples a radius mask on a 1 km radar grid, so even
#: 100 m cannot change an answer. A phone reports seven decimals and a paste from a mapping site
#: often carries six; keeping them would be storing precise personal location data that no part of
#: this system reads. Rounding happens here, at the edge, so no route can store more by accident
#: (GDPR data minimisation, DESIGN.md 13).
COORD_DECIMALS = 4


def _round_coord(value: float) -> float:
    return round(value, COORD_DECIMALS)


class SubscribeRequest(BaseModel):
    # allow_inf_nan=False: json.loads accepts the bare token NaN, and a NaN latitude that reaches
    # the database is re-evaluated every cycle forever (SECURITY_REVIEW.md F-3).
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    #: EmailStr also rejects RFC 2606 special-use domains (.invalid, .test, .localhost). That is
    #: wanted: an address we can never deliver to is one we should never store, and a bounce we
    #: can predict is a bounce we should not generate.
    #: Required for email, absent for web push.
    email: EmailStr | None = None
    channel: Literal["email", "webpush"] = "email"
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    #: The three values `PushSubscription.toJSON()` gives the page, for `channel="webpush"`.
    #: Capped at `MAX_ADDRESS_LENGTH`, the same constant `ManageLinkRequest.address` uses - the two
    #: must agree or a subscriber can exist who cannot reach their own settings.
    #:
    #: The cap is not what makes an endpoint safe: that is the host allowlist in notify/webpush.py,
    #: applied in subscriptions.subscribe, because a URL rather than a length is what can hurt us.
    #: It is here to bound an unauthenticated request body, an order of magnitude above the ~250
    #: characters the real push services issue.
    endpoint: str | None = Field(default=None, max_length=MAX_ADDRESS_LENGTH)
    #: Base64url, unpadded, as the browser produces them. 87 and 22 characters in practice. The
    #: bounds are loose enough that a padded or otherwise longer encoding of the same key is not
    #: refused here - the real check is whether they decode to a usable key at send time - but they
    #: are exactly the widths of the columns these land in, and that is not a coincidence.
    #:
    #: They used to be 256 and 128 against `String(128)` and `String(64)`. A key of 129 characters
    #: therefore passed validation and overflowed the column, which is a `DataError` out of the
    #: flush: an unauthenticated 500 from a public endpoint, reachable by anyone willing to send a
    #: long string. "Looser than the storage" is not leniency, it is a validator that hands the
    #: database input it cannot hold. `test_push_key_bounds_match_the_columns` keeps the two equal.
    #: The charset is checked too, not only the length. These two are compared against the stored
    #: pair in `subscriptions.subscribe`, and a constant-time comparison of `str` refuses non-ASCII
    #: input rather than guessing an encoding - so one umlaut here was an unhandled `TypeError`,
    #: i.e. another unauthenticated 500, found by review on top of the length one. `same_secret`
    #: now compares bytes so that path is safe regardless, but a key outside base64url cannot be a
    #: browser's key under any encoding, and the honest answer to one is 422 rather than a silent
    #: mismatch.
    p256dh: str | None = Field(
        default=None, max_length=PUSH_P256DH_MAX_LENGTH, pattern=r"^[A-Za-z0-9_=-]+$"
    )
    auth: str | None = Field(
        default=None, max_length=PUSH_AUTH_MAX_LENGTH, pattern=r"^[A-Za-z0-9_=-]+$"
    )

    _round = field_validator("lat", "lon")(_round_coord)

    @model_validator(mode="after")
    def _address_matches_channel(self) -> SubscribeRequest:
        if self.channel == "email":
            if not self.email:
                raise ValueError("email is required for the email channel")
            if self.endpoint or self.p256dh or self.auth:
                raise ValueError("the email channel does not take a push subscription")
            return self
        if self.email:
            # Refused rather than ignored: silently dropping an address someone supplied is how
            # they end up believing it was stored.
            raise ValueError("the webpush channel does not take an email address")
        if not (self.endpoint and self.p256dh and self.auth):
            # All three or none. Two of them is a row that can be stored and never delivered to,
            # which looks like a working subscription from both ends.
            raise ValueError("webpush needs endpoint, p256dh and auth")
        return self


class LocationRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    _round = field_validator("lat", "lon")(_round_coord)


class RuleRequest(BaseModel):
    """A partial update: every field optional, absent means "leave it alone".

    The real bounds live in `svc.validate_rule`, which reads them from settings - these are only
    the outer sanity limits, so that a number far outside any conceivable range is refused before
    it reaches a float conversion. Repeating the exact bounds here would be two places to change.
    """

    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    threshold_mm_5min: float | None = Field(default=None, gt=0, le=1000)
    lead_time_minutes: int | None = Field(default=None, ge=0, le=1000)
    radius_m: int | None = Field(default=None, ge=0, le=100_000)


class LocateRequest(BaseModel):
    """The signed reference a warning's link carries, handed back to be resolved."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(default="", max_length=512)


class ManageRequestByToken(BaseModel):
    """The durable token from a notification button, handed back to ask for the real link."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(default="", max_length=512)


class ManageLinkRequest(BaseModel):
    """Who to send a settings link to, in the same shape the subscribe form uses."""

    model_config = ConfigDict(extra="forbid")

    channel: Literal["email", "webpush"] = "email"
    #: The same bound as `SubscribeRequest.endpoint`, from the same constant.
    #:
    #: The bug was the *disagreement*: this was 2048 while `endpoint` was unbounded, so a browser
    #: issuing a longer endpoint could subscribe successfully and then get a 422 every time it asked
    #: for a settings link - a subscriber with no route into their own settings. Removing this bound
    #: fixed the disagreement and left two unbounded fields, which does not follow: a ceiling is what
    #: stops an unauthenticated request body being arbitrarily large, and review pointed out that
    #: "they disagreed" is not an argument for having none. One constant, comfortably above the ~250
    #: characters a real endpoint runs to.
    address: str = Field(min_length=1, max_length=MAX_ADDRESS_LENGTH)


def create_app(
    settings: Settings | None = None,
    session_factory=None,
    notifier: Notifier | None = None,
    overlay_store: OverlayStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    session_factory = session_factory or make_session_factory(make_engine(settings.database_url))
    notifier = notifier or build_notifier(settings.notifier, settings)
    if overlay_store is None and settings.overlay_dir:
        overlay_store = LocalOverlayStore(settings.overlay_dir)
    elif overlay_store is None and settings.overlay_bucket:
        overlay_store = GCSOverlayStore(
            settings.overlay_bucket, settings.overlay_public_base_url or ""
        )

    app = FastAPI(title="RainAlert", docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.notifier = notifier

    def get_session():
        with session_factory() as session:
            yield session

    # Said once, at boot, where `make logs` shows it - rather than leaving the first person to
    # click something to discover it as a 500. Never fatal: a database that is merely down at
    # start-up must not stop the process, and neither must a diagnostic.
    try:
        with session_factory() as session:
            complaint = schema_complaint(session)
        if complaint:
            logger.error("DATABASE SCHEMA IS OUT OF DATE: %s", complaint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not check the database schema at start-up: %s", exc)

    def deliver(message) -> bool:
        """Send, and never let a delivery failure become the caller's problem.

        The state change is already committed by this point, so raising here would 500 a request
        that actually succeeded - and on the subscribe path it would also leak, through the status
        code, whether a mail was attempted at all. A failure is logged and counted; the address is
        not logged.

        `message` may also be a callable returning one, and on every path where the message is built
        from request data it should be. Building is as failure-prone as sending: the builders format
        an address the caller chose, and `OutboundMessage.__post_init__` rejects some of them. Built
        at the call site, that happened *outside* this try - which is how an endpoint containing a
        newline produced a 500 after `subscribe` had already committed, leaving a row nothing could
        later deliver to or delete. The endpoint check now refuses that input, and this makes the
        same shape of bug stop being a 500 next time.
        """
        try:
            if callable(message):
                message = message()
            result = app.state.notifier.send(message)
        except Exception:  # preparing or delivering must never break the request
            # `message` is still the callable if the build was what failed, hence getattr.
            logger.exception(
                "notifier raised while preparing or sending %r",
                getattr(message, "subject", "<never built>"),
            )
            return False
        if not result.ok:
            logger.error("delivery failed: %s", result.error)
        return result.ok

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Report validation failures without echoing the input back.

        FastAPI's default handler includes the offending value, and serialising a NaN raises
        ("Out of range float values are not JSON compliant") - so a bare NaN in the body turned a
        clean 422 into a 500. Dropping `input` and `ctx` fixes that and stops the endpoint
        reflecting arbitrary user input into its own response.
        """
        reasons = [
            {
                "loc": list(err.get("loc", ())),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        # Logged, because it was not, and that cost a day.
        #
        # A subscriber on Chrome got "Das hat nicht geklappt" and the only thing the service
        # recorded was `POST /api/v1/subscriptions 422`. Which of six validation paths it was could
        # not be told from the outside - the reason existed, was returned to the browser, and was
        # thrown away by us. Diagnosing it needed DevTools on the reader's own phone, which is not
        # something a reader will ever do.
        #
        # Field names and messages only. `loc` and `msg` say "p256dh failed the pattern"; neither
        # carries the value, so an endpoint - which identifies a subscriber - stays out of the log.
        logger.warning(
            "%s %s rejected: %s",
            request.method,
            request.url.path,
            "; ".join(f"{'.'.join(str(p) for p in r['loc'])}: {r['type']}" for r in reasons),
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": reasons},
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # A per-request nonce, so the pages can keep their small inline scripts without
        # 'unsafe-inline'. Templates read it as request.state.csp_nonce.
        #
        # This is also the fix for a real bug: an earlier revision set `default-src 'self'` with no
        # script-src, which silently blocked the subscribe page's own inline script in any browser
        # that enforces CSP. The test asserted the header was present, not that the page still
        # worked - which is the difference between testing the assertion and testing the behaviour.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            # No third-party origin in either of these. Leaflet used to come from unpkg, which
            # meant every visitor announced their IP to a CDN before the map drew anything. It is
            # vendored under static/vendor/leaflet now (M6), so both directives are back to
            # 'self': every script and stylesheet a page loads is served by this app.
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; "
            # The service worker is ours and served from this origin. `worker-src` is what Chrome
            # checks for `navigator.serviceWorker.register`; without it the registration is
            # refused by `default-src`, and the only symptom is that no notification ever arrives.
            "worker-src 'self'; "
            "manifest-src 'self'; "
            # Both configured image sources, not just the tiles. The overlays are PNGs on
            # whatever OVERLAY_PUBLIC_BASE_URL names - a GCS bucket in production - and with
            # only 'self' allowed the browser blocked every one of them: the map drew an empty
            # frame and then nothing, with the radar working perfectly behind it. Local
            # development never showed it because LocalOverlayStore serves them from this app,
            # which *is* 'self'.
            #
            # `blob:` for the vector map (D-58): MapLibre decodes images through blob URLs
            # where `createImageBitmap` is missing.
            f"img-src 'self' data: blob: {image_origin(settings.map_tile_url)} "
            f"{image_origin(settings.overlay_public_base_url or '')}; "
            # Also the vector map: MapLibre fetches its tiles, and the radar overlays it
            # draws, with fetch() rather than <img> - so the tile server and the overlay bucket
            # have to be here as well as in img-src. The bucket already allows this site in its
            # CORS policy, which fetch() needs and <img> did not.
            f"connect-src 'self' {image_origin(settings.vector_tile_url)} "
            f"{image_origin(settings.overlay_public_base_url or '')}; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        # Without this the token in a confirm URL leaks to any third-party resource the page loads.
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    # The `applicationServerKey` the browser needs on subscribe. Derived here rather than stored
    # as a second setting: one key, one source. Empty when no VAPID key is configured - the signup
    # page then says push is unavailable instead of offering a button that cannot work.
    vapid_public_key = ""
    if settings.vapid_private_key:
        try:
            # Closed explicitly: `WebPushNotifier` opens an `httpx.Client` in its constructor and
            # this one exists only to do a public-key derivation, so without this it leaks a
            # connection pool for the lifetime of the process.
            probe = WebPushNotifier(
                vapid_private_key=settings.vapid_private_key,
                vapid_subject=settings.vapid_subject,
            )
            try:
                vapid_public_key = probe.application_server_key
            finally:
                probe.close()
        except Exception:
            # Logged rather than raised: the map, the radar and the email channel all work without
            # push, and a service that refuses to start because one channel is misconfigured takes
            # the others down with it.
            logger.exception("VAPID key unusable - the push channel is disabled")

    # ---- health ---------------------------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness only. Deliberately touches nothing: see /readyz."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(session: Session = Depends(get_session)) -> dict[str, str]:
        """Readiness, which by definition opens a database connection.

        That makes it an amplifier: unauthenticated traffic here scales instances and can exhaust
        the connection ceiling the ingest job needs. `--max-instances` is the control, and it is a
        deploy-time setting, not something this handler can fix (SECURITY_REVIEW.md F-6).
        """
        session.execute(sql_text("SELECT 1"))
        # Reachable is not the same as usable. New code on an un-migrated database connects
        # fine and then 500s on the first request that touches what the migration added, a long
        # way from the cause - so this names the cause.
        #
        # It does NOT keep a behind-schema revision from taking traffic, whatever this comment
        # used to say. The service's startup probe is `/healthz` (infra/run.tf), not this route,
        # so a revision whose migration has not run yet becomes ready and serves. That is what
        # makes "apply, then run the migrate job" a workable order - the apply is what puts the
        # new migration into the migrate job's image - and it is why skipping the migrate step is
        # not caught by the deploy. Wiring this in as the probe would catch it, at the price of
        # reversing that order: the migrate job's image would have to be updated on its own first.
        # The false claim here was repeated to the operator as advice before anyone checked it.
        complaint = schema_complaint(session)
        if complaint:
            logger.error("not ready: %s", complaint)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"database schema is out of date: {complaint}"
            )
        return {"status": "ready"}

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request, session: Session = Depends(get_session)) -> Response:
        """Prometheus metrics, behind a bearer token.

        Cloud Run has no notion of an internal-only route, so "internal" has to be expressed in the
        request. With no token configured the endpoint 404s - indistinguishable from not existing,
        which is the right answer for a monitoring surface nobody has set up yet.
        """
        if not settings.metrics_token:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        offered = request.headers.get("authorization", "")
        # `same_secret` rather than `secrets.compare_digest`: a header is bytes on the wire and
        # Starlette decodes it as latin-1, so a non-ASCII `Authorization` value made this line
        # raise TypeError - a 500 where the answer should plainly be 401.
        if not same_secret(offered, f"Bearer {settings.metrics_token}"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
        return Response(render_metrics(session, settings), media_type="text/plain; version=0.0.4")

    # ---- subscribe ------------------------------------------------------------------------
    @app.post("/api/v1/subscriptions", status_code=status.HTTP_202_ACCEPTED)
    def create_subscription(
        payload: SubscribeRequest,
        request: Request,
        session: Session = Depends(get_session),
    ) -> JSONResponse:
        if payload.channel == "email" and not settings.email_channel_enabled:
            # Refused before the rate limiter, and before anything is stored: the answer does
            # not depend on who is asking, and an address accepted here would wait forever for
            # a confirmation nothing can send.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "E-Mail ist derzeit nicht verfügbar – bitte Push aufs Handy wählen.",
            )

        ip = client_ip(request, settings.trusted_proxy_hops)
        within_ip = hit_and_check(
            session, f"subscribe:ip:{ip}", settings.subscribe_limit_per_hour, timedelta(hours=1)
        )
        # Per-address as well as per-IP. For email this is what stops one mailbox being mailed
        # repeatedly from many IPs. For web push the endpoint plays the same role: a browser
        # re-subscribing is normal, a thousand POSTs naming one endpoint is not - and an endpoint
        # is the only per-subscriber thing a push flood would have in common.
        within_address = True
        if payload.email:
            within_address = hit_and_check(
                session,
                f"subscribe:email:{hash_address('email', payload.email).hex()}",
                settings.subscribe_limit_per_hour,
                timedelta(hours=1),
            )
        elif payload.endpoint:
            within_address = hit_and_check(
                session,
                f"subscribe:webpush:{hash_address('webpush', payload.endpoint).hex()}",
                settings.subscribe_limit_per_hour,
                timedelta(hours=1),
            )
        if not (within_ip and within_address):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts, try later")

        try:
            result = svc.subscribe(
                session,
                settings,
                channel=Channel(payload.channel),
                address=payload.email or payload.endpoint,
                push_p256dh=payload.p256dh,
                push_auth=payload.auth,
                lat=payload.lat,
                lon=payload.lon,
                client_ip=ip,
                user_agent=request.headers.get("user-agent"),
            )
        except svc.ValidationError as exc:
            # Same reasoning as the handler above: the message names the rule that refused (a host
            # not on the allowlist, a malformed URL), never the endpoint itself.
            logger.warning("subscribe refused: %s", exc)
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

        if result.confirm_token and result.address:
            subscriber = session.get(Subscriber, result.subscriber_id)
            deliver(
                lambda: confirmation_message(
                    settings,
                    result.address,
                    result.confirm_token,
                    channel=payload.channel,
                    subscriber=subscriber,
                )
            )

        if payload.channel == "webpush":
            if result.already_active:
                # This browser was already confirmed, so no test notification is coming and the
                # spinner would wait for one forever. The location has been moved by `subscribe`
                # (the caller owns this endpoint, so the request is theirs to make), which is what
                # somebody re-signing up from the front page almost always means.
                return JSONResponse(
                    {
                        "status": "schon angemeldet - Ort aktualisiert",
                        "channel": "webpush",
                        "already_active": True,
                    },
                    status_code=status.HTTP_202_ACCEPTED,
                )
            # Nothing to hand back. Under ntfy this returned the topic, a QR code and two links,
            # because the subscriber could not be reached until they had subscribed their app to a
            # name only we knew. A browser needs none of that: it already holds the subscription it
            # just gave us, and the confirmation is on its way to it. The page's job after this is
            # to wait for the notification, not to display a credential.
            return JSONResponse(
                {"status": "warte auf die Benachrichtigung", "channel": "webpush"},
                status_code=status.HTTP_202_ACCEPTED,
            )
        # Identical response either way: no account enumeration.
        return JSONResponse({"status": "check your email"}, status_code=status.HTTP_202_ACCEPTED)

    def page(request: Request, template: str, extra: dict | None = None, **kwargs):
        """Render a page with the context every page needs.

        `has_map` is site-wide because the navigation uses it: there is no radar page to link to
        when no overlay store is configured. Injected here rather than per route, so a page added
        later cannot quietly ship without it - which is exactly how `/confirm`, `/unsubscribe`
        and `/privacy` came to have no way out of them at all.
        """
        context = {
            "settings": settings,
            "has_map": overlay_store is not None,
            # Derived from the private key rather than configured beside it, so the two cannot
            # drift. A mismatch is invisible: the browser subscribes happily and every later send
            # is rejected as unauthorised. Empty when no key is configured, which is what the page
            # checks before offering push at all.
            "vapid_public_key": vapid_public_key,
        }
        context.update(extra or {})
        response = TEMPLATES.TemplateResponse(request, template, context, **kwargs)
        # Every page here is per-request, and two things make that more than a preference.
        #
        # The CSP nonce. Each response carries a fresh one in both the header and the markup
        # (see the middleware above), and they are only useful together: a shared cache handing
        # one visitor's body to another either breaks every script on the page or, worse, hands
        # out a nonce an injected inline script could then claim. `private` is what says "no
        # shared cache", and it is the half that matters once anything sits in front of Cloud Run
        # - a CDN, a load balancer, a corporate proxy.
        #
        # And `/confirmed` sets the session cookie, so its body is specific to one subscriber.
        #
        # `private, no-cache` rather than `no-store`, deliberately. `no-store` would also do the
        # job, and it costs the back/forward cache: Chrome refuses bfcache for a `no-store`
        # document, so every Back into this page would re-run the whole script - the map, the
        # timeline fetch, the subscription check - instead of restoring instantly. `no-cache`
        # still forces revalidation on every visit, which is all that was wanted; `private` does
        # the part that protects the nonce.
        response.headers["Cache-Control"] = "private, no-cache"
        return response

    # ---- confirm --------------------------------------------------------------------------
    @app.get("/confirm", response_class=HTMLResponse, include_in_schema=False)
    def confirm_page(request: Request) -> HTMLResponse:
        """Renders a button. Changes nothing - a mail scanner may follow this freely.

        Takes no `token` parameter: the page reads it from the fragment, and accepting one from
        the query string as well would leave the shape D-26 exists to remove still working, and
        still logged, for anyone who sent such a URL.
        """
        return page(request, "confirm.html")

    @app.post("/confirm", response_class=HTMLResponse, include_in_schema=False)
    def confirm_submit(
        request: Request, token: str = Form(""), session: Session = Depends(get_session)
    ) -> HTMLResponse:
        try:
            result = svc.confirm(session, settings, token=token)
        except svc.ValidationError:
            # The service's own messages are English, which is right for the API and wrong on a
            # German page. Saying the same thing for expired and already-used is deliberate:
            # both are fixed by asking for a new one, and neither needs confirming to a stranger.
            return page(
                request,
                "error.html",
                {
                    "message": "Dieser Bestätigungslink gilt nicht mehr. Er läuft nach "
                    f"{settings.confirm_token_ttl_hours} Stunden ab und kann nur einmal benutzt "
                    "werden – melde dich einfach noch einmal an.",
                },
                status_code=400,
            )
        subscriber = session.get(Subscriber, result.subscriber_id)
        # No anchor notification any more (D-45). It existed because an ntfy topic was an
        # unmemorable string the reader had to keep somewhere, so the message itself was the
        # bookmark - and a web push notification cannot be a bookmark, because it is gone the
        # moment it is swiped and Android keeps no history by default. What replaces it: this
        # browser holds the session cookie set below, and every warning carries an Einstellungen
        # button. Confirming is also the last step that needs the channel to prove anything.

        response = page(
            request,
            "confirmed.html",
            {
                "api_token": result.api_token,
                "unsubscribe_token": result.unsubscribe_token,
                "channel": subscriber.channel.value if subscriber else Channel.EMAIL.value,
            },
        )
        # Confirming *is* the proof the settings page asks for. Reaching this line means a token
        # we sent to the channel came back, which is exactly what redeeming a magic link proves -
        # so making them go and fetch a second one would be ceremony, not security. The session
        # is the ordinary one: same length, same wall, same cookie.
        set_session_cookie(response, result.subscriber_id)
        return response

    # ---- authenticated API ----------------------------------------------------------------
    def current_subscriber(
        request: Request, session: Session = Depends(get_session)
    ) -> tuple[Subscriber, Session]:
        """Two credentials, one identity.

        A `Bearer` token is the long-lived key handed out at confirmation, for an app. The
        settings-page session is a signed cookie that expires in half an hour. They authenticate
        the same subscriber and reach the same endpoints; what differs is CSRF, because only one
        of them is sent by the browser automatically. A bearer token has to be attached by script
        that has already read it, so a cross-site request cannot carry it. A cookie rides along on
        any request the browser makes, so a cookie-authenticated **write** must also present the
        CSRF value from the page (F-16).
        """
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            try:
                subscriber = svc.resolve_token(session, token=header[7:], purpose=TokenPurpose.API)
            except svc.ValidationError as exc:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised") from exc
            return subscriber, session

        claims = session_claims(request)
        if claims is None:
            if header:
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            presented = verify_csrf_token(request.headers.get(CSRF_HEADER, ""), settings.secret_key)
            # Must match this session, not merely be a valid signature: otherwise anyone with a
            # session of their own holds a CSRF value good against everybody else's.
            if presented is None or presented.subscriber_id != claims.subscriber_id:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or stale form token")

        subscriber = session.get(Subscriber, claims.subscriber_id)
        if subscriber is None:
            # Signed, unexpired, and the account is gone - deleted since the session opened.
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
        return subscriber, session

    def session_claims(request: Request):
        """The settings-page session's claims, or None. Never raises - callers decide."""
        cookie = request.cookies.get(MANAGE_COOKIE, "")
        return verify_session_token(cookie, settings.secret_key) if cookie else None

    def rule_bounds() -> dict:
        """The limits the page renders its inputs from, so the numbers live in one place."""
        return {
            "threshold_min": settings.min_threshold_mm_5min,
            "threshold_max": settings.plausibility_max_mm_5min,
            "lead_min": settings.min_lead_minutes,
            "lead_max": settings.max_lead_minutes,
            "lead_step": svc.LEAD_STEP_MINUTES,
            "radius_max": settings.max_radius_m,
            # The same bands the map is drawn and the legend labelled from, so "warn me at
            # orange" means one thing on both pages (radar/overlay.py INTENSITY_BANDS).
            "intensity_bands": legend(),
        }

    @app.get("/api/v1/subscriptions/me")
    def read_me(current=Depends(current_subscriber)) -> dict:
        subscriber, session = current
        sub = session.query(Subscription).filter_by(subscriber_id=subscriber.id).one()
        return {
            "channel": subscriber.channel.value,
            "address": subscriber.address,
            "status": sub.status.value,
            "lat": sub.lat,
            "lon": sub.lon,
            "location_updated_at": sub.location_updated_at.isoformat(),
            "radius_m": sub.radius_m,
            "threshold_mm_5min": float(sub.threshold_mm_5min),
            "lead_time_minutes": sub.lead_time_minutes,
            "timezone": sub.timezone,
            "health_note": sub.health_note,
            "bounds": rule_bounds(),
        }

    @app.patch("/api/v1/subscriptions/me", status_code=status.HTTP_204_NO_CONTENT)
    def patch_me(
        payload: RuleRequest, request: Request, current=Depends(current_subscriber)
    ) -> Response:
        """Threshold, lead time and radius. Absent fields are left alone."""
        subscriber, session = current
        ip = client_ip(request, settings.trusted_proxy_hops)
        if not hit_and_check(
            session, f"settings:ip:{ip}", settings.settings_limit_per_hour, timedelta(hours=1)
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many updates")
        try:
            svc.update_rule(
                session,
                settings,
                subscriber,
                threshold_mm_5min=payload.threshold_mm_5min,
                lead_time_minutes=payload.lead_time_minutes,
                radius_m=payload.radius_m,
            )
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.put("/api/v1/subscriptions/me/location", status_code=status.HTTP_204_NO_CONTENT)
    def put_location(
        payload: LocationRequest, request: Request, current=Depends(current_subscriber)
    ) -> Response:
        subscriber, session = current
        ip = client_ip(request, settings.trusted_proxy_hops)
        if not hit_and_check(
            session, f"location:ip:{ip}", settings.location_limit_per_hour, timedelta(hours=1)
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many updates")
        try:
            svc.update_location(session, subscriber, lat=payload.lat, lon=payload.lon)
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.delete("/api/v1/subscriptions/me", status_code=status.HTTP_204_NO_CONTENT)
    def delete_me(current=Depends(current_subscriber)) -> Response:
        subscriber, session = current
        address, channel = subscriber.address, subscriber.channel.value
        # Read before the delete: the receipt is the last thing this channel ever gets, and after
        # `delete_subscriber` the row it needs the encryption keys from is gone.
        keys = push_keys(subscriber)
        svc.delete_subscriber(session, subscriber)
        deliver(lambda: deletion_receipt(settings, address, channel=channel, push=keys))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    # ---- settings-page session --------------------------------------------------------------
    def set_session_cookie(response: Response, subscriber_id, deadline: int | None = None) -> dict:
        """Open or renew a session. Returns what the page needs to show and use it.

        ``deadline`` carries the wall forward on a renewal. Fresh sessions get one measured from
        now; without it a renew button would make the short session in D-25 a formality.
        """
        now = datetime.now(UTC)
        if deadline is None:
            deadline = int(
                (now + timedelta(minutes=settings.manage_session_max_minutes)).timestamp()
            )
        token = session_token(
            subscriber_id, settings.secret_key, settings.manage_session_ttl_minutes, deadline, now
        )
        claims = verify_session_token(token, settings.secret_key, now)
        response.set_cookie(
            MANAGE_COOKIE,
            token,
            max_age=claims.seconds_left(now),
            httponly=True,
            samesite="lax",
            # Only when the site is actually served over TLS. Setting it unconditionally makes
            # the cookie vanish in development, which looks like a broken login, not a policy.
            secure=settings.public_base_url.startswith("https://"),
            path="/",
        )
        return session_state(claims, now)

    def session_state(claims, now: datetime | None = None) -> dict:
        """What the page shows and sends back.

        The CSRF value is minted against the session's own expiry rather than a lifetime of its
        own. Given its own clock the two drift: every page load used to mint a fresh thirty
        minutes while the session's expiry stayed put.
        """
        now = now or datetime.now(UTC)
        return {
            "csrf": csrf_token(claims.subscriber_id, settings.secret_key, claims.expires),
            "seconds_left": claims.seconds_left(now),
            "seconds_until_deadline": claims.seconds_until_deadline(now),
            "session_minutes": settings.manage_session_ttl_minutes,
        }

    @app.post("/api/v1/manage/link", status_code=status.HTTP_202_ACCEPTED)
    def request_manage_link(
        payload: ManageLinkRequest, request: Request, session: Session = Depends(get_session)
    ) -> dict:
        """Send a settings link to a channel that has already been confirmed.

        Always 202, whether or not the address is known. The endpoint takes an address someone
        typed and sends a message to it, so telling the caller which addresses exist would turn
        the settings page into a subscriber-list oracle - and the addresses are people's
        mailboxes.
        """
        ip = client_ip(request, settings.trusted_proxy_hops)
        within_ip = hit_and_check(
            session, f"manage:ip:{ip}", settings.manage_link_limit_per_hour, timedelta(hours=1)
        )
        # Per-address as well as per-IP, exactly as POST /subscriptions does it.
        #
        # This route had only the IP limiter, while `manage_link_limit_per_hour` described itself as
        # "deliberately as tight as signing up: it is the same mail-bomb lever as POST
        # /subscriptions". It was not as tight, and the missing half is the half that survives IP
        # rotation. Demonstrated: 40 POSTs from 40 addresses in `X-Forwarded-For`, all 202, 40
        # messages delivered to one subscriber.
        #
        # The flood is the obvious harm and the smaller one. `issue_manage_token` deletes the
        # subscriber's previous *unused* token, so a stranger who knows an address can invalidate
        # that person's real settings link as fast as they can request one - and for a push
        # subscriber the settings page is the only route to "Abmelden und meine Daten löschen".
        # An attacker could hold someone's deletion right shut indefinitely.
        #
        # Keyed on the hash of whatever was typed, so it is not an oracle: an unknown address is
        # counted and refused identically to a known one, and the route still answers 202 either way.
        within_address = hit_and_check(
            session,
            f"manage:address:{hash_address(payload.channel, payload.address).hex()}",
            settings.manage_link_limit_per_hour,
            timedelta(hours=1),
        )
        if not (within_ip and within_address):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests")

        channel = Channel(payload.channel)
        subscriber = svc.find_subscriber(session, channel=channel, address=payload.address)
        # Unconfirmed subscribers are excluded: confirmation is what proves the channel reaches
        # the person, and a settings link is not the place to take that on trust.
        if subscriber is not None and subscriber.confirmed_at is not None:
            token = svc.issue_manage_token(session, settings, subscriber)
            deliver(
                lambda: manage_link_message(
                    settings,
                    subscriber.address,
                    token,
                    subscriber.id,
                    channel=subscriber.channel.value,
                    subscriber=subscriber,
                )
            )
        return {"status": "check your messages"}

    @app.post("/api/v1/locate")
    def locate(
        payload: LocateRequest, request: Request, session: Session = Depends(get_session)
    ) -> dict:
        """Resolve a warning's link to the place that warning was about.

        This exists so the coordinates do not have to be in the link. A warning stays in a
        notification list for good; a screenshot of one carrying a home address would be a worse
        leak than anything else on this service, and the message itself names a time and an
        intensity but never a place.

        The token stops verifying after `locate_link_ttl_minutes`, and an expired one is answered
        exactly like a forged one - `located: false`, nothing else. Distinguishing them would
        tell a holder that the subscription behind an old link still exists.
        """
        ip = client_ip(request, settings.trusted_proxy_hops)
        if not hit_and_check(
            session, f"locate:ip:{ip}", settings.settings_limit_per_hour, timedelta(hours=1)
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests")

        claims = verify_locate_token(payload.token, settings.secret_key)
        if claims is None:
            return {"located": False}
        subscription = session.execute(
            select(Subscription).where(Subscription.subscriber_id == claims.subscriber_id)
        ).scalar_one_or_none()
        if subscription is None:
            return {"located": False}
        return {
            "located": True,
            "lat": float(subscription.lat),
            "lon": float(subscription.lon),
            "radius_m": int(subscription.radius_m),
        }

    @app.post("/api/v1/manage/request", status_code=status.HTTP_202_ACCEPTED)
    def request_manage_link_by_token(
        payload: ManageRequestByToken, request: Request, session: Session = Depends(get_session)
    ) -> dict:
        """The notification button: hand back the durable token, get the real link on the channel.

        Since D-45 this is the route back into settings for a browser that has lost its cookie but
        still holds its push subscription. It used to exist so that changing a setting did not
        begin with copying a generated topic out of the ntfy app; the token identifies the
        subscriber, it does not admit anyone, and the link it triggers goes to the subscriber's own
        channel - so holding a copy buys nothing that receiving the notification did not already
        buy (tokens.py).

        What a copy *could* buy is noise on someone else's phone, so the cap is per subscriber
        and not only per IP: the button is tapped from whatever network the phone is on, and an
        IP counter alone would be counting the wrong thing.

        Always 202, and deliberately not "that token is invalid": the answer must not tell a
        holder whether the subscription behind an expired token still exists.
        """
        ip = client_ip(request, settings.trusted_proxy_hops)
        if not hit_and_check(
            session, f"manage:ip:{ip}", settings.manage_link_limit_per_hour, timedelta(hours=1)
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests")

        claims = verify_manage_request_token(payload.token, settings.secret_key)
        if claims is None:
            return {"status": "check your messages"}
        if not hit_and_check(
            session,
            f"manage:req:{claims.subscriber_id}",
            settings.manage_request_limit_per_hour,
            timedelta(hours=1),
        ):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many requests")

        subscriber = session.get(Subscriber, claims.subscriber_id)
        if subscriber is not None and subscriber.confirmed_at is not None:
            link = svc.issue_manage_token(session, settings, subscriber)
            deliver(
                lambda: manage_link_message(
                    settings,
                    subscriber.address,
                    link,
                    subscriber.id,
                    channel=subscriber.channel.value,
                    subscriber=subscriber,
                )
            )
        return {"status": "check your messages"}

    @app.post("/api/v1/manage/session")
    def open_manage_session(
        response: Response, token: str = Form(""), session: Session = Depends(get_session)
    ) -> dict:
        """Spend the magic link, set the session cookie, hand back the CSRF value."""
        try:
            subscriber = svc.redeem_manage_token(session, token=token)
        except svc.ValidationError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        return set_session_cookie(response, subscriber.id)

    @app.get("/api/v1/manage/csrf")
    def manage_csrf(request: Request) -> dict:
        """Hand the page a form token for a session it already holds.

        Safe to serve on a GET: it requires the session cookie, and the same-origin policy stops
        another site from reading the response - which is the same thing that makes the value
        worth anything in the first place.
        """
        claims = session_claims(request)
        if claims is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
        return session_state(claims)

    @app.post("/api/v1/manage/extend")
    def extend_manage_session(request: Request, response: Response) -> dict:
        """Put the session back to its full length, up to the wall it started with.

        A deliberate action rather than a side effect of activity: the page shows the time left
        and the person decides. That keeps "this session ends at a predictable moment" true,
        which sliding-on-every-request would not.

        Needs the CSRF value like any other write - extending a credential's life is a change,
        and it is exactly the sort of thing another site would like to do on a visitor's behalf.
        """
        claims = session_claims(request)
        if claims is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authorised")
        presented = verify_csrf_token(request.headers.get(CSRF_HEADER, ""), settings.secret_key)
        if presented is None or presented.subscriber_id != claims.subscriber_id:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "missing or stale form token")
        if claims.seconds_until_deadline() <= 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "this session has reached its limit; ask for a new link"
            )
        return set_session_cookie(response, claims.subscriber_id, claims.deadline)

    @app.post("/api/v1/manage/logout", status_code=status.HTTP_204_NO_CONTENT)
    def close_manage_session() -> Response:
        """Ends the session on this device. No credential needed - it only ever removes one."""
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(MANAGE_COOKIE, path="/")
        return response

    # ---- unsubscribe ----------------------------------------------------------------------
    @app.get("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
    def unsubscribe_page(request: Request) -> HTMLResponse:
        """Side-effect free: a prefetching client must not be able to delete an account.

        No `token` parameter here either, for the reason on `confirm_page`.
        """
        return page(request, "unsubscribe.html")

    @app.post("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
    def unsubscribe_submit(
        request: Request, token: str = Form(""), session: Session = Depends(get_session)
    ) -> HTMLResponse:
        subscriber_id = verify_unsubscribe_token(token, settings.secret_key)
        subscriber = session.get(Subscriber, subscriber_id) if subscriber_id else None
        if subscriber is None:
            return page(
                request,
                "error.html",
                {"message": "Dieser Abmeldelink ist nicht gültig."},
                status_code=400,
            )
        address, channel = subscriber.address, subscriber.channel.value
        keys = push_keys(subscriber)  # see delete_me: the row is gone a line later
        svc.delete_subscriber(session, subscriber)
        deliver(lambda: deletion_receipt(settings, address, channel=channel, push=keys))
        return page(request, "unsubscribed.html")

    # ---- map timeline ---------------------------------------------------------------------
    @app.get("/api/v1/overlays/timeline")
    def overlays_timeline(
        past_hours: int | None = None, session: Session = Depends(get_session)
    ) -> dict:
        """The slider manifest: −past_hours … +2 h, with gaps named explicitly.

        Unauthenticated: it is public radar imagery, the same data anyone can fetch from DWD, and
        it carries nothing subscriber-specific. Cached briefly so a page refresh is cheap.
        """
        if overlay_store is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "overlays are not configured")
        return build_timeline(session, settings, overlay_store, past_hours)

    # ---- pages ----------------------------------------------------------------------------
    #: What the range picker under the map offers. Every one of them is inside what DWD retains;
    #: the page drops any that exceed the configured maximum rather than showing a choice that
    #: would be silently clamped.
    WINDOW_CHOICES = (3, 6, 12, 24, 48)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request, hours: str | None = None) -> HTMLResponse:
        """The radar and the signup form, on one page.

        `/map` used to be separate and is gone - not redirected. It was still in development and
        only its author had links to it, so the honest move was to delete it rather than keep a
        second address alive forever.

        `hours` is resolved here rather than in the page's JavaScript so the window is clamped
        before it reaches the browser: the script may override it from this browser's stored
        preference, but it cannot widen it past what the server is willing to serve.

        Taken as a string and parsed leniently on purpose. Declared as `int`, FastAPI answers
        ?hours=abc with a 422 validation page; a rubbish query parameter should not cost someone
        the whole page when there is a perfectly good default to fall back to.
        """
        window = settings.timeline_default_hours
        pinned = False
        if hours is not None:
            try:
                window = int(hours)
                # Only a *usable* value pins the window. `?hours=abc` falling back to the default
                # must not also announce itself as a deliberate choice, or it would override the
                # reader's stored preference with a typo.
                pinned = True
            except ValueError:
                window = settings.timeline_default_hours
        window = min(max(window, 1), settings.timeline_past_hours)
        return page(
            request,
            "index.html",
            {
                "map_engine": map_engine(),
                "layer_opacity": LAYER_OPACITY,
                "window_hours": window,
                "window_pinned": pinned,
                "choices": [c for c in WINDOW_CHOICES if c <= settings.timeline_past_hours],
            },
        )

    def map_engine() -> str:
        """Which library draws the maps: MapLibre on vector tiles wherever a tile server is
        configured (D-59), Leaflet otherwise. The pages fall back to Leaflet by themselves where
        MapLibre cannot run."""
        return "vector" if settings.vector_tile_url else "leaflet"

    @app.get("/map-style/{theme}.json", include_in_schema=False)
    def map_style(theme: str) -> JSONResponse:
        """A basemap style for the vector map, with this deployment's URLs filled in (D-58).

        The committed styles (static/map/, built by scripts/map-style/build.mjs) leave the tile
        server as a placeholder and name their fonts by static-file path. Both are resolved here:
        the tile server is a setting, and the fonts get content-versioned URLs like every other
        static file (assets.py), so a font update reaches browsers that cached the old one.
        """
        if theme not in MAP_THEMES or not settings.vector_tile_url:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        style = map_style_template(theme)
        for source in style["sources"].values():
            if source.get("type") == "vector":
                source.pop("url", None)  # a template, never a TileJSON address
                source["tiles"] = [settings.vector_tile_url]
        for faces in style.get("font-faces", {}).values():
            for face in faces:
                face["url"] = static_url(face["url"])
        return JSONResponse(style, headers={"Cache-Control": "no-cache"})

    @app.get("/manage", response_class=HTMLResponse, include_in_schema=False)
    def manage_page(request: Request) -> HTMLResponse:
        """The settings page. Renders the same shell whether or not anyone is signed in.

        Deliberately side-effect free and identical for everyone: the magic link's token is in
        the URL *fragment*, which the browser never sends, so the server cannot know at render
        time whether this request carries one. The page asks.
        """
        return page(
            request,
            "manage.html",
            {"bounds": rule_bounds(), "layer_opacity": LAYER_OPACITY, "map_engine": map_engine()},
        )

    #: Served from the root, not from /static. A service worker's default scope is the directory
    #: it was served from, so /static/sw.js could only control /static/* - it would register
    #: without complaint and then never receive a push for a notification shown on any real page.
    #: `Service-Worker-Allowed` would be the other way; a root path is simpler and has no header
    #: to forget.
    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> Response:
        return Response(
            (Path(__file__).parent / "static" / "sw.js").read_text(encoding="utf-8"),
            media_type="text/javascript",
            # A stale service worker is a subscriber who stops being warned, and the browser will
            # happily keep one for 24 hours. `no-cache` makes it revalidate on every check.
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> Response:
        """What lets Android offer "install" and gives the notification a name of its own.

        Without it a Chrome notification is labelled with the bare origin - on `*.a.run.app` that
        reads like a machine, not a rain service (Q-1 is still open). With it an installed web app
        shows `short_name` instead.
        """
        return JSONResponse(
            {
                # German, because `short_name` is what Android prints above every notification and
                # what an installed icon is labelled with. "RainAlert" is the repository's name and
                # appears nowhere a reader can see: they sign up on a page headed "Regenwarnung" and
                # would have got notifications from something else.
                "name": "Regenwarnung - Warnung vor Regen",
                "short_name": "Regenwarnung",
                "description": "Warnt, kurz bevor es an deinem Ort anfaengt zu regnen.",
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "lang": "de",
                "background_color": "#ffffff",
                "theme_color": "#1f6fb2",
                # Separate `any` and `maskable` entries. One icon claiming both was wrong: a
                # maskable icon has to keep everything meaningful inside the centre circle of 40%
                # radius, and the rain bars reached 27 units from centre on a 64-unit canvas against
                # a 25.6 budget - so Android's adaptive-icon shapes would clip them. The maskable
                # PNG is the same drawing inset by 12%.
                "icons": [
                    {"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"},
                    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {
                        "src": "/static/icon-maskable-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "maskable",
                    },
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
    def privacy(request: Request) -> HTMLResponse:
        # The map servers every visitor's browser contacts, by host, so the page names what this
        # deployment actually uses rather than what it used when the text was written (D-59).
        return page(
            request,
            "privacy.html",
            {
                "vector_tile_host": urlparse(settings.vector_tile_url).hostname or "",
                "raster_tile_host": urlparse(settings.map_tile_url or "").hostname or "",
            },
        )

    # The pages' own scripts. Served from 'self', which the CSP already allows, so the shared
    # geolocation helper does not have to be inlined into three templates and drift between them.
    app.mount(
        "/static",
        VersionedStaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    if settings.overlay_dir:
        # Development convenience. In production the overlays live in GCS behind a CDN, and the
        # raw archives must not share that prefix (SECURITY_REVIEW.md F-9).
        app.mount(
            "/overlays",
            StaticFiles(directory=settings.overlay_dir, check_dir=False),
            name="overlays",
        )

    return app
