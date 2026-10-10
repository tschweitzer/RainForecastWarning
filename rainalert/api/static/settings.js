/* The settings, on the start page (D-67).
 *
 * This was the inline script of /manage. It moved here when the settings moved under the start
 * page's map, for the reasons signup.js gives for being a file: it can be linted, cached and tested
 * as what the browser is handed. Configuration arrives as data attributes on <body>.
 *
 * It owns the settings section and the requests behind it - opening (a device key, a settings link
 * in `#t=`, or an email session), saving, deleting, and asking for a link. It does not own the map
 * or decide what the page shows: signup.js does both, and hands this file the few things it needs
 * from them as `page` in `begin(page)`:
 *
 *   page.showSettings()                  reveal the settings, hide signup and state B
 *   page.place(lat, lon, radius, zoomIn) put the editable pin there (and zoom in)
 *   page.setRadius(metres)               resize the circle around it
 *   page.hasMap()                        false when no map library loaded
 *   page.ownSubscription()               this browser's usable push subscription, or null
 *
 * and calls `RainSettings.moved(lat, lon)` when the pin moves while the settings are open.
 */
(function () {
  'use strict';

  var data = document.body.dataset;
  var LINK_TTL = parseInt(data.linkTtl, 10) || 15;
  /* Rendered by the server, not left to devicekey.js: see confirm.html. */
  var CLIENT = data.deviceKeyClient || '';

  if (window.RainKey) { window.RainKey.syncFromPage(parseInt(data.serverTime, 10)); }

  /* With a device key (D-64) every request is signed and there is no session at all: no cookie,
     no CSRF value, no countdown. Without one - email, or a browser that cannot keep a key - the
     cookie session as before. */
  var keyMode = false;
  var csrf = null;               // set when a session opens; required on every write
  var expiresAt = null;          // epoch ms; the page counts down to it
  var deadlineAt = null;         // the wall this session may not be renewed past
  var sessionMinutes = 30;
  var ticker = null;
  var current = null;            // the last state the server confirmed
  var page = null;

  function $(id) { return document.getElementById(id); }

  // ---- asking for a link ------------------------------------------------------------------
  /* The answer goes to the status line next to the button that was pressed, and the button is
     disabled for the duration: `/api/v1/manage/link` limits per address as well as per IP, so an
     impatient double-tap spends two of this subscriber's links for the hour. Re-enabled on every
     branch, because a button stuck disabled on the only route into someone's settings is worse
     than the double-tap. */
  async function sendLink(channelName, address, button, out) {
    function answer(message) {
      out.textContent = message;
      if (button) { button.disabled = false; }
    }
    if (button) { button.disabled = true; }
    out.textContent = 'Wird gesendet …';
    var response;
    try {
      response = await fetch('/api/v1/manage/link', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({channel: channelName, address: address})
      });
    } catch (error) {
      answer('Keine Verbindung. Bitte versuch es noch einmal.');
      return;
    }
    if (response.status === 429) {
      /* The cause is deliberately not named: the per-address half of the limit can fire for
         requests that did not come from this connection at all, and telling the reader which half
         tripped would tell a stranger the same thing. */
      answer('Zu viele Anfragen in der letzten Stunde. Bitte in etwa einer '
        + 'Stunde noch einmal versuchen – an deinen Eingaben liegt es nicht.');
      return;
    }
    if (!response.ok) {
      answer('Bei uns ist gerade etwas schiefgegangen. Bitte später noch einmal.');
      return;
    }
    if (channelName === 'webpush') {
      /* No hedge on this route, and it says where to look: nobody can type a push endpoint, so
         there is nothing to hide, and the link arrives as a notification, not on this page. */
      answer('Die Benachrichtigung mit dem Link ist unterwegs – schau in deine '
        + 'Benachrichtigungen. Der Link gilt ' + LINK_TTL + ' Minuten und kann nur einmal '
        + 'benutzt werden.');
      return;
    }
    // Email: the same answer whether or not the address is known, because the server answers the
    // same way. Anything else here would tell a stranger who has signed up.
    answer('Wenn dieser Kanal angemeldet ist, ist der Link unterwegs. '
      + 'Er gilt ' + LINK_TTL + ' Minuten und nur einmal.');
  }

  // State B: a push subscription this browser cannot prove yet. One link registers a key.
  var toBrowser = $('link-to-browser');
  if (toBrowser) {
    toBrowser.addEventListener('click', async function () {
      var out = $('link-to-browser-result');
      var subscription = page ? await page.ownSubscription() : null;
      if (!subscription) {
        out.textContent = 'Dieser Browser ist nicht mehr für Benachrichtigungen angemeldet. '
          + 'Lade die Seite neu und melde dich neu an.';
        return;
      }
      sendLink('webpush', subscription.endpoint, toBrowser, out);
    });
  }

  var emailForm = $('link-form');
  if (emailForm) {
    emailForm.addEventListener('submit', function (event) {
      event.preventDefault();
      sendLink('email', $('link-address').value, emailForm.querySelector('button[type=submit]'),
        $('link-result'));
    });
  }

  // ---- the session (cookie, without a device key) -------------------------------------------
  function adoptSession(state) {
    csrf = state.csrf;
    // Derived from the server's countdown rather than from a timestamp, so a browser clock that
    // is wrong - and plenty are - does not show a wrong answer.
    expiresAt = Date.now() + state.seconds_left * 1000;
    deadlineAt = Date.now() + state.seconds_until_deadline * 1000;
    sessionMinutes = state.session_minutes;
    showTimeLeft();
    if (!ticker) { ticker = setInterval(showTimeLeft, 1000); }
  }

  function showTimeLeft() {
    var left = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
    var label = $('session-left');
    var button = $('extend');
    if (left <= 0) {
      label.textContent = 'Die Sitzung ist abgelaufen – lade die Seite neu und fordere einen '
        + 'neuen Link an.';
      button.hidden = true;
      clearInterval(ticker);
      ticker = null;
      return;
    }
    var minutes = Math.floor(left / 60);
    var seconds = left % 60;
    label.textContent = 'Sitzung endet in ' + minutes + ':'
      + String(seconds).padStart(2, '0') + ' ';
    // Offered only when there is room left before the wall, so the button never promises
    // something the server will refuse.
    button.hidden = deadlineAt - Date.now() < 60000;
    button.textContent = 'Auf ' + sessionMinutes + ' Minuten verlängern';
  }

  $('extend').addEventListener('click', async function () {
    var response = await fetch('/api/v1/manage/extend', {
      method: 'POST', headers: {'X-Rain-CSRF': csrf}
    });
    if (!response.ok) {
      $('session-left').textContent = response.status === 409
        ? 'Diese Sitzung hat ihre Höchstdauer erreicht – fordere einen neuen Link an.'
        : 'Die Sitzung konnte nicht verlängert werden.';
      $('extend').hidden = true;
      return;
    }
    adoptSession(await response.json());
  });

  // ---- redeeming a link ---------------------------------------------------------------------
  /* Spends the link. Beside the token it sends proof that this browser holds the push
     subscription the link was sent to, and a fresh device key (devicekey.js, D-64). Answers
     'key' (a key was registered: no session), 'session' (the cookie session), 'mismatch' (this
     browser is not the one the link belongs to - and the link was not spent) or 'spent'.

     Reports, does not decide. A spent link is only the end of the road if this browser has no
     key and no session either - opening the same link in a second tab spends it there, and
     answering "dieser Link gilt nicht mehr" to somebody who is still signed in is both wrong and
     baffling, because reloading then works. */
  async function redeem(token) {
    var body = new URLSearchParams();
    body.set('token', token);
    body.set('client', CLIENT);
    if (window.RainKey) {
      var fields = await window.RainKey.prepareRedemption();
      Object.keys(fields).forEach(function (name) { body.set(name, fields[name]); });
    }
    var response;
    try {
      response = await fetch('/api/v1/manage/session', {method: 'POST', body: body});
    } catch (error) {
      return 'spent';
    }
    if (response.status === 403) {
      var refused = await response.json().catch(function () { return {}; });
      if (refused.detail && refused.detail.error === 'push_mismatch') { return 'mismatch'; }
    }
    if (!response.ok) {
      if (window.RainKey) { window.RainKey.dropPending(); }
      return 'spent';
    }
    var answer = await response.json();
    if (answer.enrolled && window.RainKey) {
      await window.RainKey.activate(answer.enrolled);
      return 'key';
    }
    if (window.RainKey) { window.RainKey.dropPending(); }
    adoptSession(answer);
    return 'session';
  }

  /* Resolves to true, or to the status that refused it (0 for no answer at all). */
  async function load() {
    var response;
    try {
      response = keyMode
        ? await window.RainKey.signedFetch('GET', '/api/v1/subscriptions/me')
        : await fetch('/api/v1/subscriptions/me');
    } catch (error) {
      return 0;
    }
    if (!response) { return 401; }
    if (!response.ok) { return response.status; }
    current = await response.json();
    return true;
  }

  /* A cookie session the server no longer accepts - its subscriber was deleted, say from the
     unsubscribe link in a mail. Not an error to report: there is simply nothing to open. */
  function dropSession() {
    csrf = null;
    if (ticker) { clearInterval(ticker); ticker = null; }
  }

  // ---- the form -----------------------------------------------------------------------------
  var threshold = $('threshold');

  function paintSwatch() {
    var option = threshold.options[threshold.selectedIndex];
    $('threshold-swatch').style.background = (option && option.dataset.rgba) || 'transparent';
  }
  threshold.addEventListener('change', paintSwatch);

  function selectThreshold(value) {
    // Numeric comparison, not string: the option values are rendered by the server as 3.0 while
    // JSON hands back 3, and `select.value = '3'` against an option value of '3.0' silently
    // selects nothing - which on save would write the first band instead.
    for (var i = 0; i < threshold.options.length; i++) {
      if (parseFloat(threshold.options[i].value) === value) {
        threshold.selectedIndex = i;
        paintSwatch();
        return;
      }
    }
    // A stored value that is not one of the bands - the 0.1 default, or anything set through the
    // API. Kept as its own option rather than snapped to a neighbour, because silently changing
    // someone's threshold while showing them their settings is the worst of both.
    var previous = threshold.querySelector('option[data-custom]');
    if (previous) { previous.remove(); }
    var extra = document.createElement('option');
    extra.dataset.custom = '1';
    extra.value = String(value);
    extra.textContent = 'eigener Wert – ab ' + value + ' mm/5 min';
    // Wearing the colour of the band it falls into, so a custom value is still placed on the
    // same scale as everything else rather than being a blank.
    for (var j = threshold.options.length - 1; j >= 0; j--) {
      var band = threshold.options[j];
      if (band.dataset.rgba && parseFloat(band.value) <= value) {
        extra.dataset.rgba = band.dataset.rgba;
        break;
      }
    }
    threshold.insertBefore(extra, threshold.firstChild);
    threshold.selectedIndex = 0;
    paintSwatch();
  }

  function showLead() {
    // The label already says "voraus schauen"; repeating it here made it wrap.
    $('lead-value').textContent = (+$('lead').value) + ' Minuten';
  }

  function showRadius() {
    // Metres below a kilometre, kilometres above it: "7500 m" is a number you have to divide.
    var m = +$('radius').value;
    $('radius-value').textContent = m < 1000
      ? m + ' m'
      : (m / 1000).toLocaleString('de-DE', { maximumFractionDigits: 2 }) + ' km';
  }

  $('lead').addEventListener('input', showLead);
  $('radius').addEventListener('input', function () {
    showRadius();
    if (page) { page.setRadius(+$('radius').value || 0); }
  });

  function fill() {
    $('settings-lat').value = current.lat;
    $('settings-lon').value = current.lon;
    selectThreshold(current.threshold_mm_5min);
    $('lead').value = current.lead_time_minutes;
    $('radius').value = current.radius_m;
    showLead();
    showRadius();

    /* Only for email: the address says whose warnings these are. For push `current.address` is
       the endpoint - 200-odd characters of URL that identify nothing to the reader. The states that
       matter are not lost with it: pending and unhealthy each put a notice in the banner below. */
    var who = $('who');
    var state = {
      active: 'aktiv', pending: 'noch nicht bestätigt',
      unhealthy: 'gestört', paused: 'pausiert'
    }[current.status] || current.status;
    who.hidden = current.channel !== 'email';
    who.textContent = who.hidden ? '' : current.address + ' · ' + state;

    /* Both notices, not whichever runs last - pending first, because it explains why nothing is
       arriving. */
    var notices = [];
    // A pending subscription is not being evaluated at all, so saying nothing here would let
    // someone tune a rule that cannot fire.
    if (current.status === 'pending') {
      notices.push('Diese Anmeldung ist noch nicht bestätigt – es werden noch keine Warnungen '
        + 'verschickt.');
    }
    if (current.health_note) { notices.push(current.health_note); }
    var banner = $('settings-banner');
    banner.hidden = notices.length === 0;
    banner.textContent = notices.join(' ');
  }

  /* The pin moved - on the map, by the map's locate button, or by the one below. Rounded to the
     four decimals the server keeps, so what is shown is what will be stored. */
  function moved(lat, lon) {
    $('settings-lat').value = lat.toFixed(4);
    $('settings-lon').value = lon.toFixed(4);
  }

  /* Beside the coordinate fields, which are only on screen when no map loaded. With a map there
     is a control on it; without one, this is the only way to avoid typing decimal degrees. */
  var locateButton = $('settings-locate');
  locateButton.addEventListener('click', function () {
    window.RainGeo.locate({
      onBusy: function (busy) { locateButton.disabled = busy; },
      onStatus: function (text, kind) {
        var status = $('settings-locate-status');
        status.textContent = text;
        status.className = kind === 'error' ? 'hint error' : 'hint';
      },
      onFound: function (lat, lon) { moved(lat, lon); }
    });
  });

  // ---- saving ------------------------------------------------------------------------------
  async function send(method, url, body) {
    if (keyMode) {
      var signed = await window.RainKey.signedFetch(method, url, body);
      // No key any more (another tab, or it was replaced): reported like any refusal.
      return signed || new Response(null, {status: 401});
    }
    return fetch(url, {
      method: method,
      headers: {'Content-Type': 'application/json', 'X-Rain-CSRF': csrf},
      body: JSON.stringify(body)
    });
  }

  function explain(response) {
    if (response.status === 401 || response.status === 403) {
      return keyMode
        ? 'Das hat nicht geklappt. Lade die Seite bitte neu.'
        : 'Die Sitzung ist abgelaufen. Lade die Seite neu und fordere einen neuen Link an.';
    }
    if (response.status === 429) {
      return 'Zu viele Änderungen in kurzer Zeit. Bitte etwas später noch einmal.';
    }
    if (response.status === 422) {
      return 'Diese Werte gehen nicht. Bitte prüfe die Eingaben.';
    }
    return 'Bei uns ist gerade etwas schiefgegangen. Bitte später noch einmal probieren.';
  }

  var result = $('settings-result');

  $('settings-form').addEventListener('submit', async function (event) {
    event.preventDefault();
    var button = event.target.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      await save();
    } catch (error) {
      result.textContent = 'Keine Verbindung. Bitte versuch es noch einmal.';
    } finally {
      button.disabled = false;
    }
  });

  async function save() {
    result.textContent = 'Wird gespeichert …';
    var lat = parseFloat($('settings-lat').value);
    var lon = parseFloat($('settings-lon').value);
    var movedPlace = lat !== current.lat || lon !== current.lon;

    // Two requests, because they are two different changes with two different consequences: a
    // move resets the alert state (D-17), a rule change does not. Sent rule-first so that a
    // refused rule does not leave the location already moved.
    var rule = await send('PATCH', '/api/v1/subscriptions/me', {
      threshold_mm_5min: parseFloat(threshold.value),
      lead_time_minutes: parseInt($('lead').value, 10),
      radius_m: parseInt($('radius').value, 10)
    });
    if (!rule.ok) { result.textContent = explain(rule); return; }

    if (movedPlace) {
      var where = await send('PUT', '/api/v1/subscriptions/me/location', {lat: lat, lon: lon});
      if (!where.ok) {
        result.textContent = 'Die Einstellungen wurden gespeichert, der Ort nicht: '
          + explain(where);
        if (await load() === true) { fill(); showPlace(false); }
        return;
      }
    }

    if (await load() === true) { fill(); showPlace(false); }
    result.innerHTML = movedPlace
      ? '<span class="ok">Gespeichert.</span> Der Ort hat sich geändert, deshalb beginnt die '
        + 'Bewertung neu – die nächste Warnung kommt erst, wenn es bei dir gerade trocken ist '
        + 'und Regen aufzieht.'
      : '<span class="ok">Gespeichert.</span>';
  }

  // ---- leaving ------------------------------------------------------------------------------
  // Two taps, and the second one is the one that deletes. See the markup for why.
  $('delete').addEventListener('click', function () {
    $('delete-confirm').hidden = false;
    $('delete').hidden = true;
  });
  $('delete-no').addEventListener('click', function () {
    $('delete-confirm').hidden = true;
    $('delete').hidden = false;
  });
  $('delete-yes').addEventListener('click', async function () {
    result.textContent = 'Wird gelöscht …';
    var response;
    try {
      response = keyMode
        ? (await window.RainKey.signedFetch('DELETE', '/api/v1/subscriptions/me')
          || new Response(null, {status: 401}))
        : await fetch('/api/v1/subscriptions/me', {
          method: 'DELETE',
          headers: {'X-Rain-CSRF': csrf}
        });
    } catch (error) {
      response = new Response(null, {status: 503});
    }
    if (!response.ok) {
      result.textContent = 'Das hat nicht geklappt. Bitte später noch einmal probieren.';
      return;
    }
    /* Server-side row gone; now the browser's own subscription, or it keeps a live endpoint that
       nothing will ever post to. Failure here is not reported: the deletion already happened, and
       an error about tidying up would read as "your data was not deleted". */
    try {
      var subscription = page ? await page.ownSubscription() : null;
      if (subscription) { await subscription.unsubscribe(); }
    } catch (error) { /* nothing the reader can do */ }
    /* And the device key: the server's half went with the subscriber, this browser's half goes
       here, so nothing is left behind that recognises a subscription that no longer exists. */
    if (window.RainKey) { await window.RainKey.forgetAll(); }
    /* A fresh page rather than a page rearranged by hand: the pin, the circle and the form all go
       back to "nobody is signed up here", and signup.js says what happened. Told through this
       tab's sessionStorage, not the URL: a `/?abgemeldet` anyone could link would announce a
       deletion above somebody's open settings. Where storage is refused, the page is simply the
       signup without the sentence. */
    try { window.sessionStorage.setItem('rainalert.deleted', '1'); } catch (error) { /* fine */ }
    window.location.replace('/');
  });

  // ---- opening ------------------------------------------------------------------------------
  function showPlace(zoomIn) {
    if (page.hasMap()) {
      page.place(current.lat, current.lon, current.radius_m, zoomIn);
      return;
    }
    // Neither map library loaded - blocked or broken rather than a setting. The coordinate fields
    // are otherwise not shown at all.
    $('settings-coord-fallback').hidden = false;
    $('settings-hint').textContent =
      'Die Karte konnte nicht geladen werden – Koordinaten bitte eintippen.';
  }

  function open(spent) {
    fill();
    // Nothing to count down with a device key: there is no session.
    $('session-end').hidden = keyMode;
    var note = $('settings-note');
    note.hidden = !spent;
    note.textContent = spent
      ? 'Dieser Link war schon benutzt – in diesem Browser bist du aber noch angemeldet.'
      : '';
    result.textContent = '';
    page.showSettings();
    showPlace(true);
  }

  /* Opens the settings if this browser may: a settings link, a device key, or a cookie session -
     in that order. Resolves to `{opened, note}`, where `note` says why not when there is something
     worth saying (a spent link, one belonging to another browser).

     `options.token`: a settings link's token. signup.js reads it from `#t=` and erases it from the
     address bar the moment the page starts deciding, before anything is awaited.
     `options.session`: whether a cookie session is possible at all. Asking costs a request on every
     visit, and without a push subscription in this browser and without email on this deployment
     there is nothing it could find - nor anywhere below to ask for a new link. A link always asks. */
  async function begin(pageApi, options) {
    page = pageApi;
    options = options || {};
    var token = options.token || '';
    var outcome = null;
    if (token) { outcome = await redeem(token); }
    if (outcome === 'mismatch') {
      // Not spent: the browser that does hold the subscription can still use the link.
      return { opened: false, note: 'Dieser Link gehört zu einer Anmeldung in einem anderen '
        + 'Browser. Öffne ihn dort – oder melde dich hier neu an.' };
    }
    var spent = outcome === 'spent';

    // The device key first: no session to find, the request proves itself (D-64). A browser
    // without one, or whose key the server no longer knows, falls through to the cookie session -
    // silently, because a missing key is normal. Asked even with a session in this tab: a key
    // registered since (a link redeemed elsewhere on this page) is the better proof.
    if (window.RainKey) {
      var me = null;
      try {
        me = await window.RainKey.signedFetch('GET', '/api/v1/subscriptions/me');
      } catch (error) { me = null; }
      if (me && me.ok) {
        keyMode = true;
        current = await me.json();
        open(spent);
        window.RainKey.rotateIfDue();
        return { opened: true, note: null };
      }
    }
    keyMode = false;
    var spentNote = spent
      ? 'Dieser Link gilt nicht mehr – er läuft nach ' + LINK_TTL + ' Minuten ab und kann nur '
        + 'einmal benutzt werden.' + (options.session ? ' Fordere unten einfach einen neuen an.' : '')
      : null;
    if (!csrf) {
      if (!token && !options.session) { return { opened: false, note: null }; }
      // There may still be a live session from a moment ago, and a reload should not cost a new
      // link. The CSRF value is deliberately not in the cookie, so it is fetched back; the cookie
      // is what authorises that, and another site cannot read the answer.
      var again = null;
      try { again = await fetch('/api/v1/manage/csrf'); } catch (error) { again = null; }
      if (!again || !again.ok) {
        // Now a spent link really is the end of the road, and only now is it worth saying so.
        return { opened: false, note: spentNote };
      }
      adoptSession(await again.json());
    }
    var loaded = await load();
    if (loaded === 401 || loaded === 403) {
      dropSession();
      return { opened: false, note: spentNote };
    }
    if (loaded !== true) {
      return { opened: false, note: 'Deine Einstellungen konnten gerade nicht geladen werden. '
        + 'Lade die Seite bitte gleich noch einmal.' };
    }
    open(spent);
    return { opened: true, note: null };
  }

  window.RainSettings = { begin: begin, moved: moved };
})();
