/* The service worker. Served from / (not /static/) so its scope covers the whole site - see the
   route in app.py for why that matters.

   Three jobs, and nothing else. This file runs in a worker with no DOM, it is the only code that
   runs when the site is closed, and a thrown exception here is a notification that never appears
   with no page to report it on. So: no dependencies, no caching, no offline shell. It is a
   notification handler, not a progressive web app framework.

   The payload it receives is built by notify/webpush.py:payload_for. The two have to agree, and
   that agreement is asserted in tests/test_webpush.py rather than left to comments. */

'use strict';

/* Chrome requires a notification to be shown for every push it delivers. Skip one and it shows
   "This site has been updated in the background" on your behalf, which is worse than anything we
   would have written. So every path here ends in showNotification, including the failure paths. */
var FALLBACK_TITLE = 'Regenwarnung';
/* Used when a payload names no tag. */
var FALLBACK_TAG = 'rainalert';
/* Must match the tag mail.py puts on the settings-link push. */
var MANAGE_TAG = 'rainalert-manage';
/* PNG, not the SVG the manifest uses: Chrome renders no SVG in a notification, on desktop or on
   Android, so an SVG here means the warning shows Chrome's own default icon instead of ours. The
   badge is separate because it is specified as a monochrome alpha mask at roughly 24px * DPR - a
   full-colour app icon there renders as a solid blob even where SVG works. */
var ICON = '/static/icon-192.png';
var BADGE = '/static/badge-96.png';

self.addEventListener('install', function () {
  /* Take over from the previous worker immediately rather than waiting for every tab to close.
     A subscriber with a stale worker is a subscriber not being warned, and they have no way to
     know they should close a tab to fix it. */
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('push', function (event) {
  var data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    /* A payload we cannot parse is our bug, not the reader's, and they still get something rather
       than Chrome's generic text. */
    data = {};
  }

  /* The id is the index, which is why payload_for must not reorder the array. `title` is what
     Android draws - uppercased, no icon, at most two of them. Sliced to `maxActions` because
     anything past it is discarded silently at display time, and a worker that asks for three
     should not depend on the platform being forgiving.

     `typeof`, not `|| 2`: a platform reporting 0 - it draws no action buttons at all, which is what
     Safari does - is falsy, so `|| 2` asked it for two. Harmless in practice, since the extras are
     ignored rather than thrown, but the point of this line is to send what the platform will draw
     and it did not do that in the one case where the number is not the default. */
  /* Both halves guarded. `typeof x.y` still throws if `x` itself is undefined, and this runs
     synchronously in the push listener *before* event.waitUntil - so a worker global without
     `Notification` would show nothing at all, on the one code path whose stated premise is
     that every path ends in showNotification. Chromium defines it (verified), and the page's
     own pushSupported() already declines to assume the global exists, which is reason enough
     not to assume it here where there is no page to report the error on. */
  var maxActions = (typeof Notification !== 'undefined'
    && typeof Notification.maxActions === 'number') ? Notification.maxActions : 2;
  var actions = (data.actions || []).slice(0, maxActions).map(
    function (action, index) {
      return { action: String(index), title: action.title };
    }
  );

  event.waitUntil(
    self.registration.showNotification(data.title || FALLBACK_TITLE, {
      body: data.body || '',
      icon: ICON,
      badge: BADGE,
      lang: 'de',
      /* Everything the click handler needs, because it gets the notification and not the push. */
      data: data,
      /* This line was missing, and its absence removed the only route a push subscriber has from a
         notification back into their settings. `actions` was computed above and then not passed,
         so no button was ever drawn - and because `NotificationEvent.action` is the empty string
         when the body is clicked, the whole action branch of `notificationclick` was dead code.
         Meanwhile `mail.py` had already dropped the unsubscribe URL from push bodies on the
         grounds that "the exit on push is the settings page, reached by the Einstellungen button
         on every warning". There was no button. */
      actions: actions,
      /* One rain warning at a time: a second replaces the first rather than stacking. Without a
         tag, a shower that keeps re-triggering leaves a column of near-identical notifications
         and the reader stops reading any of them.

         From the payload, because a single tag for every kind of message meant a settings link, or
         this worker's own acknowledgement, replaced a live warning and its map link at the moment
         the reader wanted it. Falls back to the old single tag when the sender does not say. */
      tag: data.tag || FALLBACK_TAG,
      renotify: true,
      requireInteraction: false
    })
  );
});

self.addEventListener('notificationclick', function (event) {
  var data = event.notification.data || {};
  event.notification.close();

  /* No action id means the body was tapped: open where the message points. */
  if (!event.action) {
    var url = data.url || '/';
    event.waitUntil(focusOrOpen(url));
    return;
  }

  var action = (data.actions || [])[Number(event.action)];
  if (!action || !action.url) {
    return;
  }

  /* A button POSTs and stays here. Nothing is opened and nothing is navigated, so the token in
     the body never reaches a URL bar or a history entry - which is the property notify/base.py
     describes and the reason these are POSTs rather than links. */
  event.waitUntil(
    fetch(action.url, {
      method: 'POST',
      headers: { 'Content-Type': action.contentType || 'application/json' },
      body: action.body || '',
      /* No cookies. This request carries its own token and is made from a worker that may be
         running with no page open; sending the settings session along would widen what a tap can
         do beyond what the token authorises. */
      credentials: 'omit'
    })
      .then(function (response) {
        if (response.ok) {
          return tell('Der Link zu den Einstellungen ist unterwegs.');
        }
        /* 429 gets its own line. "Bitte später noch einmal" is what the reader can act on; the old
           message told them to try again now, against a route that will refuse them for an hour. */
        if (response.status === 429) {
          return tell('Zu viele Anfragen. Bitte in etwa einer Stunde noch einmal.');
        }
        /* "Tippe hier", not "öffne die Seite direkt": the reader is holding a phone and has no
           address to type, and this notification's own `data.url` already points at /manage - so
           tapping it does exactly the thing the old wording asked them to do by hand. */
        return tell('Das hat nicht geklappt. Tippe hier, um die Einstellungen zu öffnen.');
      })
      .catch(function () {
        return tell('Keine Verbindung. Tippe hier, sobald du wieder online bist.');
      })
  );
});

/* No `pushsubscriptionchange` handler, deliberately.
 *
 * A browser can rotate an endpoint, and when it does the old one starts answering 410 and we delete
 * a subscriber who never unsubscribed. This event is the notice. There was a handler here that
 * re-subscribed and POSTed the new endpoint to `/api/v1/push/resubscribe`, and it was removed for
 * two reasons that compound.
 *
 * The endpoint it would have authenticated with is not a secret that proves anything. Knowing
 * somebody's endpoint does not let a third party push to them - the push service rejects a send
 * whose VAPID signature does not match the key the subscription was made with, so only we can push.
 * That makes an endpoint more like a username than a password, and an API that moves a subscription
 * on the strength of one is an API that redirects a stranger's warnings to an endpoint of your
 * choosing. Those warnings carry a locate reference and a settings token, so the endpoint handed
 * over a home address, which is the single thing this service is built not to leak.
 *
 * And it would have worked rarely. Firefox fires this event with neither `oldSubscription` nor
 * `newSubscription` populated, and Chrome only started populating them recently; without
 * `oldSubscription.options.applicationServerKey` there is no way for this worker to learn the key
 * it must re-subscribe with, so the handler returned without doing anything in exactly the cases it
 * existed for.
 *
 * What happens instead, and it is the whole of it: the next send to the rotated endpoint gets 410,
 * the subscriber row is deleted (dispatcher.deliver_queued, or the weekly liveness run), and the
 * reader subscribes again - losing their settings, which is the cost D-47 already states and
 * accepts. A bearer-string API that leaks a location is not a good trade for avoiding that.
 *
 * Note there is no third defence. An earlier version of this comment claimed "the page also
 * re-POSTs its subscription on every load"; nothing does. index.html POSTs only from the submit
 * handler, and manage.html only asks for a settings link.
 */

function focusOrOpen(url) {
  /* Reuse a tab that is already on the site rather than opening a third copy of it - but only when
     doing so actually loads the target.
     
     The trap: map.html reads its `#l=` token in a load-time script and then replaceState's the hash
     away, so an open tab's URL is plain `/map`. Navigating that tab to `/map#l=<new token>` differs
     only in the fragment, which is a *same-document* navigation - no script re-runs, the new token
     is never read, and the reader taps their second warning and gets the country view or the stale
     view from the first one. Verified in Chromium: one locate call for two navigations.
     
     So a tab is only navigated when the path differs. Same path, different fragment, gets a fresh
     window instead, which does run the script. */
  return self.clients
    .matchAll({ type: 'window', includeUncontrolled: true })
    .then(function (windows) {
      for (var i = 0; i < windows.length; i++) {
        var client = windows[i];
        if (client.url.indexOf(self.registration.scope) !== 0 || !('focus' in client)) {
          continue;
        }
        /* No same-path special case any more, and this is the interesting part.
        
           There used to be one: map.html read its `#l=` token once at load and replaceState'd the
           hash away, so navigating an open `/map` tab to `/map#l=<new token>` was a same-document
           navigation - no script re-ran, the token was never read, and the reader got the stale
           view. The branch here broke out of the loop so `openWindow` ran instead.
        
           map.html now listens for `hashchange` and re-reads the token, so `navigate()` works. With
           both halves in place the branch had become not just redundant but harmful: it matched on
           *every* warning after the first, so each one opened another tab - warning 3 of an
           afternoon shower left three copies of the map open. Verified in node: two navigations to
           the same path with different hashes opened two windows.
        
           One fix, on the page side, where the token is actually read. */
        return client.navigate(url).then(function (navigated) {
          return (navigated || client).focus();
        });
      }
      return self.clients.openWindow(url);
    })
    .catch(function () {
      return self.clients.openWindow(url);
    });
}

function tell(message) {
  /* A button that does something invisible feels broken, and the reader has no page to look at.
     Short, and never an error the reader cannot act on.

     Not titled FALLBACK_TITLE ('Regenwarnung'): this is an acknowledgement, not a warning, and a
     notification saying "Regenwarnung" that is not one is exactly the kind of thing that teaches a
     reader to stop trusting the real ones.

     And tagged 'rainalert', not 'rainalert-ack', so the settings link that arrives a moment later
     replaces it. Under its own tag the reader was left holding two notifications - this one and the
     link - with no way to tell which was which; tapping this one opens /manage with no token, lands
     on the gate, and they ask for another link, which produces a third. */
  return self.registration.showNotification('Einstellungen', {
    body: message,
    icon: ICON,
    badge: BADGE,
    lang: 'de',
    /* The settings family's tag, so the link that follows replaces this acknowledgement - and so
       neither of them evicts a rain warning. */
    tag: MANAGE_TAG,
    data: { url: '/manage' }
  });
}
