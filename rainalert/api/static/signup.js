/* The signup page's own JavaScript.
 *
 * A file rather than an inline block, and the reason is the bug history. As a nonce-inline script
 * it could not be linted, could not be cached, and could only be tested by pulling functions out of
 * rendered HTML with a brace matcher. Three bugs reached production through that gap: a reference to
 * a deleted constant that killed signup outright, a `var` read before its assignment that told every
 * visitor their browser could not do push, and a block that was copied instead of moved so the page
 * shipped two of everything. None of them was visible to a Python test, and the first two would have
 * been visible to a linter.
 *
 * Configuration arrives as data attributes on <body> rather than as Jinja interpolated into the
 * source, which is what lets this be a static file at all. See `config()` below.
 */

/* Everything the template used to interpolate. Read once, so a typo in an attribute name fails
   here rather than at the point of use. */
function config() {
  var d = document.body.dataset;
  return {
    hasOverlay: d.hasOverlay === 'true',
    tileUrl: d.tileUrl || '',
    tileAttribution: d.tileAttribution || '',
    radius: parseInt(d.radius, 10) || 2000,
    layerOpacity: parseFloat(d.layerOpacity),
    vapidKey: d.vapidKey || '',
    emailAvailable: d.emailAvailable === 'true',
    windowHours: parseInt(d.windowHours, 10) || 12,
    maxHours: parseInt(d.maxHours, 10) || 48,
    windowPinned: d.windowPinned === 'true',
    // 'vector' for the MapLibre trial (`/?karte=vektor`, D-58), otherwise 'leaflet'.
    mapEngine: d.mapEngine || 'leaflet'
  };
}
/* `const`, not `var`, and that is the whole point of this line.

   Three bugs in this file have been the same bug: something that runs during page setup read a
   module constant declared further down, `var` hoisted the declaration without the assignment, and
   the read returned `undefined` instead of throwing. Each one failed silently and in a different
   direction - every visitor told their browser could not do push, every returning subscriber shown
   the signup form again, a stored preference written correctly and never once read back. None was
   caught by a test; all three were caught by driving a browser, and only because someone happened
   to look at the right thing.

   `const` has a temporal dead zone: the same mistake throws where it happens instead of yielding
   `undefined`. Measured, by moving `WINDOW_KEY` back below its use: the page setup stops there, so
   the range picker, the legend and the slider never appear at all - broken in a way the first
   person to load the page cannot miss, rather than a radar that silently shows the wrong window.
   This file already needs ES2017 for `async`/`await`, so it costs no browser that could have run
   it anyway. Use it for anything at this level that setup code might read. */
const CONFIG = config();

/* Where this browser remembers the reader's chosen radar window.

   Declared here, at the top, and not next to the two functions that use it. It was down there,
   and `var` hoists the declaration without the assignment - so the map setup, which runs earlier
   in this file, called `storedWindow()` while `WINDOW_KEY` was still `undefined`, read the
   localStorage key literally named "undefined", got null every time and silently fell back to the
   default. The preference was written correctly and never once read back. Caught in Chromium, not
   by a test, which is the third bug of exactly this shape in this file - hence the position and
   hence `test_the_pages_own_javascript_behaves` now exercising `storedWindow()` directly.

   localStorage access is wrapped by both users below, because reading it is not safe: Safari in
   private browsing and any browser with site data blocked *throw* on access rather than returning
   null, and this runs during page setup. An uncaught throw here would take the map and the signup
   form with it, to remember a slider position. A reader who cannot store the preference gets the
   default every visit, which is the old behaviour and fine. */
const WINDOW_KEY = 'rainalert.windowHours';


/* A signup that has gone through is not a form any more.
 *
 * Left as it was, the button stayed live directly above the result - and pressing it again
 * mints a second subscription with a second topic, silently replacing the one on screen. On a
 * desktop that was the likely next move rather than an unlucky one: the result began at y=868
 * of a 900-pixel viewport and the page did not scroll, so one press looked like nothing had
 * happened.
 */
function settled(pending) {
  document.getElementById('signup').hidden = true;
  // Says what will happen when you sign up; you have, and the steps above now say it better.
  var standing = document.getElementById('what-happens');
  if (standing) { standing.hidden = true; }

  var again = document.createElement('p');
  again.className = 'alt';
  var restart = document.createElement('a');
  restart.href = '/';
  restart.textContent = 'Von vorn anfangen';
  again.appendChild(document.createTextNode('Etwas falsch gemacht? '));
  again.appendChild(restart);
  /* `pending` decides the rest of the sentence, and it used to be unconditional. An
     already-confirmed browser re-signing up - the "I moved" case, which is the usual way anyone
     reaches that branch - was told "diese Anmeldung verfällt von selbst, wenn du sie nicht
     bestätigst". There is nothing to confirm and nothing expires, so the reader waited for a
     notification that was never coming and then followed "Von vorn anfangen" back into the same
     branch. A loop, built out of one sentence that was true for only one of the two outcomes. */
  again.appendChild(document.createTextNode(
    pending === false
      ? ' – deine Anmeldung bleibt dabei bestehen.'
      : ' – diese Anmeldung verfällt von selbst, wenn du sie nicht bestätigst.'
  ));
  document.getElementById('result').appendChild(again);

  document.getElementById('result').scrollIntoView({ block: 'start', behavior: 'smooth' });
}

var pick = null, map = null;

function setPlace(lat, lon, fromMap) {
  // The hidden fields stay the one place the coordinates live, so the submit handler does not
  // have to know whether they came from the map, the locate button or the fallback inputs.
  document.getElementById('lat').value = lat.toFixed(4);
  document.getElementById('lon').value = lon.toFixed(4);
  if (pick && !fromMap) { pick.set(lat, lon, true); }
  var hint = document.getElementById('map-hint');
  hint.textContent = 'Ort gesetzt. Zum Verschieben antippen oder den Pin ziehen.';
  // Clears the error styling a failed submit may have left on it, or "Ort gesetzt" arrives in
  // red and reads as another complaint.
  hint.className = 'hint';
}
/* One map, two jobs.

   Everything that does not depend on who is looking - the basemap, the radar loop, the legend and
   the range picker - is built immediately, because the map is the page. Only the parts that
   change a subscription (the draggable pin, and the locate button setting it) wait for the state
   check below, so a subscriber never sees a pin they are not allowed to move.

   `has_map` says whether there is radar imagery to lay over the map. Picking a place does not
   need any: the basemap - tiles, or the graticule and city dots - is what you aim with. Only the
   loop is conditional. */
var radar = null;

/* Which library draws the map: the same set of functions either way (`createMap`, `picker`,
   `timeline`, ...), so nothing below needs to know.

   The vector trial only where it can actually run. `RainRadarGL` is defined by radar-gl.js, a
   module that does not define it without WebGL and cannot run at all without module support or
   if MapLibre failed to load - and each of those falls back to Leaflet, which the trial page
   loads too, rather than to an empty box. */
function chooseEngine() {
  if (CONFIG.mapEngine === 'vector' && typeof RainRadarGL !== 'undefined') { return RainRadarGL; }
  if (typeof L !== 'undefined' && typeof RainRadar !== 'undefined') { return RainRadar; }
  return null;
}
var Engine = null;

(function () {
  Engine = chooseEngine();
  if (!Engine) {
    // No Leaflet: the map area would be an empty box, so take it away and offer the fields.
    document.getElementById('map').hidden = true;
    document.getElementById('map-hint').hidden = true;
    document.getElementById('where-label').textContent = 'Wo soll gewarnt werden? (Koordinaten)';
    document.getElementById('coord-fallback').hidden = false;
    return;
  }

  // Opens on the whole country: a new visitor has not told us anything yet, so any closer view
  // would be a guess, and a guess here is a wrong location nobody notices. Fitted to Germany
  // rather than set to a fixed zoom (D-55); how, is in `createMap`.
  const germany = [[47.27, 5.87], [55.06, 15.04]];
  map = Engine.createMap('map', { bounds: germany, padding: 12 });
  Engine.basemap(map, {
    tileUrl: CONFIG.tileUrl,
    tileAttribution: CONFIG.tileAttribution,
    graticule: true
  });

  openOnTheWarningsPlace();
  /* And again whenever the fragment changes, which is the case that was broken.

     The service worker prefers to reuse an open tab, and that function erases the hash as its
     first act - so a tab left over from an earlier warning sits at plain `/`. Sending it to
     `/#l=<new token>` changes only the fragment, which is a same-document navigation: no script
     re-runs, the new token is never read, and the reader taps their second warning and gets the
     country view with no explanation. Verified in Chromium: one locate call for two navigations.

     This listener IS the fix, and the direction matters: `focusOrOpen` in sw.js navigates an open
     tab on purpose and depends on this to re-read the token. An earlier version had the worker
     dodge the problem by opening a new window instead, but the dodge matched every warning after
     the first, so an afternoon of showers left a column of map tabs, and it was removed. It also
     covers a reader following two warning links by hand. */
  window.addEventListener('hashchange', openOnTheWarningsPlace);

  if (!CONFIG.hasOverlay) { return; }

  var legend = Engine.legendControl();
  map.addControl(legend);

  var controls = document.getElementById('radar-controls');
  controls.hidden = false;
  radar = Engine.timeline(map, {
    pastHours: initialWindow(),
    layerOpacity: CONFIG.layerOpacity,
    slider: document.getElementById('slider'),
    play: document.getElementById('play'),
    stamp: document.getElementById('frame-stamp'),
    legend: legend.body(),
    banner: document.getElementById('banner'),
    // Under the pin, or the radar hides the thing being positioned.
    behindMarkers: true,
    onEmpty: function () {
      controls.hidden = true;
      document.getElementById('frame-stamp').textContent = '';
    },
    onReady: function () { controls.hidden = false; }
  });

  rangePicker();
})();

/* Which window the slider opens on.

   Three sources, in this order: `?hours=` in the URL, this browser's stored preference, the
   server's default. A shared link wins over the preference on purpose - someone sending "look at
   the last 48 hours" is not asking about your settings - and the server has already clamped it,
   so a hand-typed `?hours=999` arrives here as the ceiling rather than as a 600-frame slider. */
function initialWindow() {
  if (CONFIG.windowPinned) { return CONFIG.windowHours; }
  var stored = storedWindow();
  return stored === null ? CONFIG.windowHours : stored;
}

function storedWindow() {
  /* The try covers the localStorage call and nothing else, which is not fussiness.

     It used to wrap this whole body, and that quietly defeated the `const` above: moving the
     declaration below this function makes reading it a `ReferenceError`, the catch swallowed it,
     `storedWindow()` returned null, and the page fell back to the default exactly as silently as
     before. A catch written for "this browser refuses site data" must not also absorb "this code
     is wrong".

     Hence the separate `key` line. Narrowing the try to the `getItem` call alone was not enough
     while `WINDOW_KEY` was still read *as its argument* - that read is inside the try, so the
     ReferenceError was still caught and the page still failed silently. Verified both ways by
     moving the declaration and watching the console. */
  var key = WINDOW_KEY;
  var raw;
  try {
    raw = window.localStorage.getItem(key);
  } catch (error) {
    return null;
  }
  if (raw === null) { return null; }
  var hours = parseInt(raw, 10);
  // Validated, not trusted: this is the only input to the page that a *previous* version of the
  // page wrote, so an old or hand-edited value must not become a range nobody offers. Anything
  // outside what the server is willing to serve falls back to the default.
  if (!isFinite(hours) || hours < 1 || hours > CONFIG.maxHours) { return null; }
  return hours;
}

function rememberWindow(hours) {
  // Both reads outside the try, for the reason spelled out in storedWindow().
  var key = WINDOW_KEY;
  var value = String(hours);
  try { window.localStorage.setItem(key, value); } catch (error) { /* fine */ }
}

/* The range picker. Buttons rather than the links the old radar page used, because the choice is
   now remembered in this browser instead of carried in the URL.

   Reloads the live timeline rather than rebuilding it: `RainRadar.timeline()` registers the
   slider and play listeners once per call, so calling it again would leave two of each and the
   next play tap would run two timers against one slider. */
function rangePicker() {
  var nav = document.getElementById('range');
  if (!nav || !radar) { return; }
  var buttons = [].slice.call(nav.querySelectorAll('button[data-hours]'));
  if (!buttons.length) { return; }
  nav.hidden = false;

  function mark(hours) {
    buttons.forEach(function (b) {
      b.setAttribute('aria-pressed', String(+b.dataset.hours === hours));
    });
  }

  mark(initialWindow());
  buttons.forEach(function (button) {
    button.addEventListener('click', function () {
      var hours = +button.dataset.hours;
      mark(hours);
      rememberWindow(hours);
      /* The cursor lands on "jetzt", which `load()` does for us: it picks the frame at offset 0.
         Keeping the old slider index would be meaningless - index 40 of a 3-hour window and index
         40 of a 48-hour window are ten hours apart - and keeping the old *time* would drop a
         reader who was watching the forecast back into the past. "Now" is the one position that
         means the same thing in every window. */
      radar.load(hours);
    });
  });
}

/* Tapped a warning? Centre on the place it was about.

   The link carries a signed reference, not the coordinates - a warning stays in a notification
   list for good, and a screenshot of one should not be somebody's address. The reference stops
   resolving after an hour, and then this is simply the ordinary map, which is the whole point:
   last week's warning tells a reader nothing about where its owner lives. */
function openOnTheWarningsPlace() {
  var hash = window.location.hash || '';
  if (hash.indexOf('#l=') !== 0) { return; }
  var token = decodeURIComponent(hash.slice(3));
  // Erased before anything else can read it, and replaceState so Back does not return to a URL
  // still carrying it.
  history.replaceState(null, '', window.location.pathname + window.location.search);
  var stale = document.getElementById('stale-link');
  stale.hidden = true;

  fetch('/api/v1/locate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: token })
  }).then(function (r) {
    return r.ok ? r.json() : { located: false };
  }).then(function (place) {
    if (!place.located) {
      // Said rather than silently ignored: a map that opens on the whole country when you
      // expected your own street reads as broken unless it says why.
      stale.hidden = false;
      return;
    }
    Engine.mark(map, place.lat, place.lon, place.radius_m);
    // 11 is what the settings map opens on - about 40 km across, enough to see which town you are
    // in and to judge a shower's distance against it.
    map.setView([place.lat, place.lon], 11);
  }).catch(function () { /* the map is still a map */ });
}

/* Does this browser already hold a subscription we could send to?

   Browser-side only, and it has to be: the server has no endpoint that answers "is this endpoint
   subscribed" without a session, and adding one would be an oracle letting anyone test whether a
   given push endpoint is registered here. So this asks the same question /manage asks, with the
   same `sameKey` bias - a subscription bound to a different applicationServerKey is not a route
   into anything, because every send signed with our current key is rejected forever.

   It therefore answers "this browser believes it is subscribed", not "the server has a confirmed
   row". A signup abandoned before confirmation leaves a live browser subscription behind, and
   that reader sees state B until the server purges it. The line state B shows is worded for
   exactly that: it says where to go, and the settings page is where the truth is. */
async function thisBrowserIsSubscribed() {
  if (!pushSupported() || !VAPID_KEY) { return false; }
  try {
    var registration = await navigator.serviceWorker.getRegistration('/');
    if (!registration) { return false; }
    var subscription = await registration.pushManager.getSubscription();
    return !!subscription && sameKey(subscription, VAPID_KEY);
  } catch (error) {
    return false;
  }
}

/* Decide which half of the page below the map the reader gets.

   Fail-open, in three places, because the failure that matters is a visitor who cannot sign up:
   the catch, the timeout, and `getRegistration` resolving to nothing all end at the signup form.
   Only a positive answer hides it.

   `serviceWorker.getRegistration`, not `.ready`: `ready` never resolves when no worker is
   registered, which is every first-time visitor - the form would have stayed hidden forever on
   exactly the browsers it exists for.

   Called at the very bottom of this script, not here, for the same reason `announceCapability()`
   is: it reads `VAPID_KEY`, which is declared further down with `var`. `var` hoists the
   declaration without the assignment, so running this in place read `undefined`, decided no
   subscription could exist, and showed every returning subscriber the signup form - which is the
   whole bug this state check was added to remove. */
function decideSignupState() {
  var section = document.getElementById('signup-section');
  var already = document.getElementById('already-subscribed');
  var settled = false;

  function reveal(subscribed) {
    if (settled) { return; }
    settled = true;
    section.hidden = !!subscribed;
    already.hidden = !subscribed;
    if (subscribed) { enableViewing(); } else { enablePicking(); }
  }

  // The floor under the whole thing. `getSubscription()` is normally a few milliseconds, but it
  // talks to the browser's push machinery and there is no contract that it ever settles; a
  // visitor staring at a map with nothing under it is a worse outcome than a subscriber seeing
  // the form for a moment.
  var guard = window.setTimeout(function () { reveal(false); }, 1500);

  thisBrowserIsSubscribed().then(function (subscribed) {
    window.clearTimeout(guard);
    reveal(subscribed);
  }).catch(function (error) {
    // Still fail-open - a visitor who cannot sign up is the outcome that matters - but no longer
    // silent. Nothing here is expected to throw, so anything that does is a bug in this file, and
    // a bug that degrades gracefully is one nobody reports. The console is where it shows, and
    // the browser harness collects console errors.
    if (window.console) { window.console.error('signup state check failed', error); }
    window.clearTimeout(guard);
    reveal(false);
  });
}

/* The pin, and the locate button that sets it. Only ever called for a reader who is not signed
   up: on this page a subscriber's location is not editable, because editing it here would be the
   settings page rebuilt in a second place. */
function enablePicking() {
  if (!map) { return; }
  pick = Engine.picker(map, {
    radius: CONFIG.radius,
    onChange: function (lat, lon) { setPlace(lat, lon, true); }
  });

  // On the map rather than under it: this is a map control, it belongs where a map keeps them,
  // and here it sets the pin rather than merely showing where the device is.
  map.addControl(Engine.locateControl({
    onStatus: function (text, kind) {
      if (kind === 'error' && text) {
        var hint = document.getElementById('map-hint');
        hint.textContent = text;
        hint.className = 'hint error';
      }
    },
    onFound: function (lat, lon) {
      setPlace(lat, lon, false);
      // 12 rather than the opening 5: having just asked to be found, you want to see the street,
      // and 12 is as far as 1 km radar lets the tile layer go.
      map.setView([lat, lon], 12);
    }
  }));
}

/* The same button for a reader who is already signed up, with the half that changes a
   subscription taken out.

   It is still here, and that is the point of the distinction: "where am I on this radar" is a map
   question, and a subscriber watching a shower approach wants it answered as much as anyone. What
   they must not get is a pin that looks draggable, because dragging it would appear to move their
   warning location and would not - that lives on the settings page.

   `RainRadar.mark` rather than the picker's pin: nothing about it is interactive, and it replaces
   its predecessor through `map.__rainalertMark`, so pressing the button twice leaves one marker
   instead of a trail. The accuracy circle it draws is honest here - at 1 km radar resolution,
   "somewhere within 2 km" and "here" look identical and only one of them is true. */
function enableViewing() {
  if (!map) { return; }
  var banner = document.getElementById('banner');
  map.addControl(Engine.locateControl({
    onStatus: function (text, kind) {
      // Reuses the page's banner rather than the signup form's hint, which is hidden in this
      // state - an error message inside a hidden section is an error nobody is told about.
      if (kind === 'error' && text) {
        banner.hidden = false;
        banner.textContent = text;
      }
    },
    onFound: function (lat, lon, accuracy) {
      banner.hidden = true;
      Engine.mark(map, lat, lon, Math.max(accuracy || 0, 50));
      map.setView([lat, lon], Math.max(map.getZoom(), 10));
    }
  }));
}// The button beside the coordinate fields, which are only on screen when Leaflet failed to
// load. With a map there is a control on it; without one, this is the only way to avoid typing
// decimal degrees, so it does not disappear with the map - it appears with the fields.
(function () {
  var button = document.getElementById('locate');
  var status = document.getElementById('locate-status');
  button.addEventListener('click', function () {
    RainGeo.locate({
      onBusy: function (busy) { button.disabled = busy; },
      onStatus: function (text, kind) {
        status.textContent = text;
        status.className = kind === 'error' ? 'hint error' : 'hint';
      },
      onFound: function (lat, lon) {
        // Four decimals because that is what the server keeps; showing seven would change
        // under them on submit.
        setPlace(lat, lon, false);
      }
    });
  });
})();
function chosenChannel() {
  var picked = document.querySelector('input[name=channel]:checked');
  return picked ? picked.value : 'webpush';
}

function syncChannelFields() {
  var isEmail = chosenChannel() === 'email';
  var field = document.getElementById('email-field');
  field.hidden = !isEmail;
  // required only when it is the channel, or the browser refuses to submit a hidden empty field
  document.getElementById('email').required = isEmail;
  // The consent text and the "what happens next" note name what is actually stored and how the
  // confirmation arrives, both of which differ by channel. An email subscriber is told about an
  // address and a mail; a push subscriber has no address and gets a notification.
  Array.prototype.forEach.call(document.querySelectorAll('.for-email'), function (node) {
    node.hidden = !isEmail;
  });
  Array.prototype.forEach.call(document.querySelectorAll('.for-push'), function (node) {
    node.hidden = isEmail;
  });
}
Array.prototype.forEach.call(
  document.querySelectorAll('input[name=channel]'),
  function (radio) { radio.addEventListener('change', syncChannelFields); }
);
syncChannelFields();

function isApplePhone() {
  /* iPadOS reports itself as Macintosh, hence the touch-points test. */
  return /iPhone|iPad|iPod/.test(navigator.userAgent)
    || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
}

/* Says up front when push cannot work here, instead of letting the reader find out after filling in
   the form. Three states are worth naming and the rest are not: this is a note, not a diagnosis. */
function announceCapability() {
  var note = document.getElementById('capability-note');
  var message = null;
  /* Push cannot work in this browser at all. The only way forward is another channel, or - on an
     iPhone - another way of opening the same page, so say which and move the reader off push. */
  var impossible = !pushSupported() || !VAPID_KEY;
  if (impossible) {
    /* Four different messages, because the right next step differs in all four cases and a
       concatenated one contradicted itself. The old iPhone text stated the Home Screen detour at
       length and then withdrew it with "Wir haben E-Mail für dich ausgewählt", and said "hier
       anmelden" when the Home Screen opens a fresh page with an empty form - so the one word naming
       where to act pointed at the wrong place in both configurations. */
    if (isApplePhone()) {
      message = EMAIL_AVAILABLE
        ? 'Wir haben E-Mail für dich ausgewählt – so bekommst du die Warnungen auch auf dem '
          + 'iPhone. Push geht dort nur als Web-App: füge die Seite über „Teilen → Zum '
          + 'Home-Bildschirm“ hinzu und melde dich dort an.'
        : 'Auf dem iPhone geht Push nur als Web-App: füge die Seite über „Teilen → Zum '
          + 'Home-Bildschirm“ hinzu und öffne sie von dort. Dort kannst du dich dann anmelden.';
    } else {
      message = EMAIL_AVAILABLE
        ? 'Dieser Browser kann keine Push-Benachrichtigungen empfangen. Wir haben E-Mail für dich '
          + 'ausgewählt.'
        /* The intended first deployment, and the only true dead end on this page: no push here and
           no second channel. It used to stop after the first sentence, leaving a map, a consent box
           and a live Anmelden that could only ever repeat the same sentence into #result. Says what
           would work, and the button goes below. */
        : 'Dieser Browser kann keine Push-Benachrichtigungen empfangen – hier kannst du dich nicht '
          + 'anmelden. In einem aktuellen Chrome oder Firefox funktioniert es.';
    }
  } else if (Notification.permission === 'denied') {
    /* Blocked, not impossible - so the channel is left alone. Chrome shows no prompt at all on a
       second request after a denial, which is why this has to be said before the button rather than
       after it, but it is two taps to undo and the reader chose push. Switching channels for them
       here would take away the thing they asked for to solve a problem they can fix. */
    message = 'Benachrichtigungen sind für diese Seite blockiert. Erlaube sie in den '
      + 'Website-Einstellungen deines Browsers (Symbol links neben der Adresse) – sonst können wir '
      + 'dich nicht warnen.';
  }
  if (!message) { return; }
  note.textContent = message;
  note.hidden = false;
  /* The bold "your browser will ask" sentence is false in both branches: there is no prompt coming
     when push is unsupported, and none coming after a denial either. */
  var prompt = document.getElementById('permission-prompt-note');
  if (prompt) { prompt.hidden = true; }
  if (impossible && EMAIL_AVAILABLE) {
    var email = document.querySelector('input[name=channel][value=email]');
    if (email) { email.checked = true; syncChannelFields(); }
  }
  if (impossible && !EMAIL_AVAILABLE) {
    /* Nothing on this page can succeed, so the button stops inviting the attempt. Only this branch:
       a *denied* permission is two taps to undo and the reader may well come back and press, so that
       button stays live - the same judgement as not switching their channel for them. */
    var submit = document.querySelector('#signup button[type=submit]');
    if (submit) { submit.disabled = true; }
  }
}
/* Called at the very bottom of this script, not here. `var VAPID_KEY` is declared further down, and
   `var` hoists the declaration without the assignment - so calling this during parse read VAPID_KEY
   as undefined and announced "this browser cannot receive push notifications" to every visitor on a
   perfectly capable browser. Found by driving Chromium; nothing in the Python suite could see it,
   and it is the second time an ordering mistake in this script has produced a confident, wrong
   page. */

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) { node.className = cls; }
  if (text) { node.textContent = text; }
  return node;
}

/* The VAPID public key, from the server. Empty when none is configured, which is the signal that
   this deployment cannot do push at all - rendered as JSON so an empty value is an empty string
   rather than a syntax error. */
const VAPID_KEY = CONFIG.vapidKey;
/* Whether there is a second channel to fall back to, which decides what a push failure may suggest. */
const EMAIL_AVAILABLE = CONFIG.emailAvailable;

/* base64url -> Uint8Array. `applicationServerKey` will not take the base64 string, only bytes,
   and atob does not know base64url - so the two substitutions and the padding are both needed.
   Getting this wrong produces an InvalidCharacterError at subscribe time and nothing else. */
function keyBytes(base64url) {
  var padded = base64url.replace(/-/g, '+').replace(/_/g, '/');
  padded += '='.repeat((4 - (padded.length % 4)) % 4);
  var raw = atob(padded);
  var bytes = new Uint8Array(raw.length);
  for (var i = 0; i < raw.length; i++) { bytes[i] = raw.charCodeAt(i); }
  return bytes;
}

function pushSupported() {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

/* Everything between "Anmelden" and having something to POST. Returns the subscription, or throws
   with a message already worded for the reader - the caller has no way to improve on it and each
   failure here needs a different next step. */
async function pushSubscription() {
  if (!VAPID_KEY) {
    /* Conditional, because the intended first deployment has email off - and then "Bitte E-Mail
       wählen" points at an option that is not on the page. "Server" also blames machinery the
       reader cannot see or fix. */
    throw new Error(EMAIL_AVAILABLE
      ? 'Push ist hier gerade nicht verfügbar. Bitte E-Mail wählen.'
      : 'Push ist hier gerade nicht verfügbar. Das liegt nicht an dir – bitte später noch '
        + 'einmal probieren.');
  }
  if (!pushSupported()) {
    /* The iPhone instruction is the answer on iOS and noise anywhere else, so it is only given
       there. Everywhere else, offer the channel that does work if there is one. */
    if (/iPhone|iPad|iPod/.test(navigator.userAgent)
        || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1)) {
      throw new Error('Auf dem iPhone geht das nur, wenn du die Seite über „Teilen → Zum '
        + 'Home-Bildschirm“ hinzufügst und sie von dort öffnest.');
    }
    throw new Error('Dieser Browser kann keine Push-Benachrichtigungen empfangen.'
      + (EMAIL_AVAILABLE ? ' Du kannst dich stattdessen per E-Mail anmelden.' : ''));
  }
  /* Permission first, before any await. Transient user activation does not survive an arbitrarily
     long await, and `serviceWorker.ready` on a first-ever registration waits for install and
     activate - so asking afterwards spends the gesture and then asks. Chrome does not enforce
     activation for requestPermission(), which is why Android-first hid this; Firefox and Safari do,
     and iOS-on-the-Home-Screen is the one platform D-45 still explicitly claims. There is nothing
     to lose by asking first: a reader who says no should not have a worker registered for them.

     Chrome treats a second call after a denial as already-denied and shows nothing, so "denied"
     needs its own wording - there is no prompt left to answer and it has to be undone in the
     browser's own UI. */
  var permission = await Notification.requestPermission();
  if (permission === 'denied') {
    /* The sliders icon left of the URL, not the padlock: the padlock is desktop Chrome, and on
       Android - the target - there is nothing there to look for. */
    throw new Error('Benachrichtigungen sind für diese Seite blockiert. Erlaube sie in den '
      + 'Website-Einstellungen deines Browsers (Symbol links neben der Adresse), dann hier noch '
      + 'einmal anmelden.');
  }
  if (permission !== 'granted') {
    /* Dismissed rather than denied: the prompt can still be shown again, so say how. */
    throw new Error('Ohne erlaubte Benachrichtigungen können wir dich nicht warnen. Tippe noch '
      + 'einmal auf „Anmelden“ und wähle „Zulassen“.');
  }

  /* Registered at the root scope, which is why app.py serves it from / and not from /static.

     Wrapped because this function promises its caller a message already worded for the reader, and
     the submit handler shows `error.message` verbatim on that promise. `register`, `ready` and
     `subscribe` all reject with raw DOMExceptions - and `subscribe` failing is routine on Android,
     where Play Services trouble gives "AbortError: Registration failed - push service error". A
     German page was showing an English internal string with no next step. */
  var registration;
  try {
    registration = await navigator.serviceWorker.register('/sw.js');
    /* `register` resolves before the worker is usable; `ready` is the one that waits for active. */
    await navigator.serviceWorker.ready;
  } catch (error) {
    throw new Error('Der Hintergrunddienst deines Browsers lässt sich nicht starten. Lade die '
      + 'Seite neu; im privaten Modus funktionieren Benachrichtigungen meist nicht.');
  }

  /* An existing subscription is reused rather than replaced: subscribe() with the same key returns
     the same endpoint, so this is idempotent, and reusing it means a second signup from the same
     browser presents the keys the server already has rather than a new pair it would refuse.

     (It is not a way back in after clearing browser data. Clearing site data unregisters the
     worker, so getSubscription() returns null and there is nothing here to reuse - which is what
     the page says on the tin and what D-47 accepts.)

     But only if it was made with the key we are signing with now. A subscription is bound to the
     applicationServerKey it was created with, and a push signed with any other key is rejected by
     the push service with a 403 - forever, silently, from the reader's side: they see a successful
     signup and are never warned. So a mismatch is replaced rather than reused. `options` is not
     populated on every browser; when we cannot tell, reuse is the safer guess, because dropping a
     working subscription costs the reader their settings. */
  var existing = await registration.pushManager.getSubscription();
  if (existing) {
    if (sameKey(existing, VAPID_KEY)) { return existing; }
    /* Best-effort: if the old subscription will not go away, subscribing again below is still the
       right move, and failing here would stop the reader for a reason they cannot act on. */
    try { await existing.unsubscribe(); } catch (error) { /* keep going */ }
  }
  try {
    return await registration.pushManager.subscribe({
      /* Required by Chrome. It is a promise that every push shows a notification, which sw.js keeps
         on every path including its failure paths. */
      userVisibleOnly: true,
      applicationServerKey: keyBytes(VAPID_KEY)
    });
  } catch (error) {
    throw new Error('Dein Browser konnte die Benachrichtigungen nicht einrichten. Versuche es '
      + 'später noch einmal – oft hilft es, den Browser neu zu starten.');
  }
}

function sameKey(subscription, expected) {
  /* Compares the bytes, not the encoding: `options.applicationServerKey` comes back as an
     ArrayBuffer and the page holds base64url. Returns true when the browser does not tell us
     (no `options`, or a null key) - see the caller for why that way round. */
  var options = subscription.options || {};
  var key = options.applicationServerKey;
  /* "Cannot tell" has to include more than null. `!key` catches null and undefined, and an empty
     ArrayBuffer is a truthy object - so a browser populating `options` with a zero-length or
     non-ArrayBuffer key compared 0 bytes against 65, called it a mismatch, and unsubscribed a
     working subscription on every single signup attempt: new endpoint, new pending row, the
     confirmed one orphaned, settings lost each time. No shipping browser does this, but the whole
     point of the fallback was to prefer keeping a subscription when we are not sure, and that is
     exactly what this case is. */
  if (!(key instanceof ArrayBuffer) || key.byteLength === 0) { return true; }
  var actual = new Uint8Array(key);
  var wanted = keyBytes(expected);
  if (actual.length !== wanted.length) { return false; }
  for (var i = 0; i < actual.length; i++) {
    if (actual[i] !== wanted[i]) { return false; }
  }
  return true;
}

/* Everything it reads - VAPID_KEY, pushSupported, EMAIL_AVAILABLE - is defined above by here. */
announceCapability();
decideSignupState();

document.getElementById('signup').addEventListener('submit', async function (event) {
  event.preventDefault();
  var out = document.getElementById('result');
  var channel = chosenChannel();

  // The coordinate fields are no longer visible, so `required` on them would refuse the submit
  // with a browser message pointing at a hidden field - which reads as the form being broken.
  // Say what is missing, next to the map where it is fixed.
  var lat = parseFloat(document.getElementById('lat').value);
  var lon = parseFloat(document.getElementById('lon').value);
  function refuse(message) {
    var hint = document.getElementById('map-hint');
    hint.textContent = message;
    hint.className = 'hint error';
    hint.hidden = false;
    hint.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }
  if (isNaN(lat) || isNaN(lon)) {
    refuse('Bitte setze zuerst deinen Ort – tippe dazu in die Karte.');
    return;
  }
  // The map lets you drop a pin anywhere, and the radar only covers Germany. Said here, next to
  // the pin, rather than left to the server: "Das hat nicht geklappt, prüfe die Eingaben" under
  // the button does not tell anyone that the problem is *where* they pointed.
  if (lat < 47 || lat > 56 || lon < 5 || lon > 16) {
    refuse('Dieser Ort liegt außerhalb Deutschlands – nur dort reicht das Radar des DWD.');
    return;
  }
  out.textContent = 'Wird gesendet …';
  /* Disabled for the duration. `settled()` closes the "press it again after it finished" hole; this
     closes "double-tap before it answers", which on a slow connection is the likelier of the two.
     Re-enabled on every path that leaves the form on screen - `finish()` below - because a button
     stuck disabled after an error is worse than the double-tap it prevented. */
  var submit = event.target.querySelector('button[type=submit]');
  if (submit) { submit.disabled = true; }
  function finish(message) {
    out.textContent = message;
    if (submit) { submit.disabled = false; }
  }

  var body = { channel: channel, lat: lat, lon: lon };
  if (channel === 'email') {
    body.email = document.getElementById('email').value;
  } else {
    /* The permission prompt happens here, not on page load. Asking before anyone has said what
       they want is how a site trains people to hit Block, and a blocked site cannot recover
       without the reader going into browser settings. */
    var subscription;
    try {
      subscription = await pushSubscription();
    } catch (error) {
      finish(error.message);
      return;
    }
    var keys = subscription.toJSON().keys || {};
    body.endpoint = subscription.endpoint;
    body.p256dh = keys.p256dh;
    body.auth = keys.auth;
  }

  var response;
  try {
    response = await fetch('/api/v1/subscriptions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
  } catch (error) {
    /* A dropped connection rejects rather than returning a status. Until the submit button started
       being disabled during the request this merely showed nothing; now it would leave a dead form,
       so the change that made the page safer against double-taps made it worse against a flaky
       link. This is the worse of the two pages for it: on the push channel the permission has been
       granted and the browser is already holding a subscription the server never heard about. */
    finish('Keine Verbindung. Bitte versuch es noch einmal – deine Anmeldung ist noch nicht '
      + 'abgeschickt.');
    return;
  }

  if (!response.ok) {
    // One message for every failure told people to check their input even when the input was
    // fine and the limiter had simply run out - which sends them round the form again, using
    // up the attempts they did not know they were short of.
    if (response.status === 429) {
      // Same correction as the settings page: subscribe limits per IP *and* per address, so
      // naming the connection as the cause is wrong whenever the address half is what tripped.
      // (Worded without quoting the old string: a test forbids it anywhere in the served page, and
      // a comment in an inline script is part of the served page. Third time on this file.)
      finish('Zu viele Anmeldeversuche in der letzten Stunde. Bitte in etwa einer '
        + 'Stunde noch einmal versuchen – an deinen Eingaben liegt es nicht.');
    } else if (response.status === 422 || response.status === 400) {
      /* One 422 is not the reader's fault and must not be blamed on them: the endpoint check can
         refuse a push service we have not listed, and then the browser did everything right, the
         subscription exists in their browser's own settings, and "prüfe die Eingaben" sends them
         round a form where there is nothing to correct. It happened - a real Chrome install handed
         out `jmt17.google.com`, which was missing from the allowlist, and the page told the reader
         to check input that was already fine.

         Our service-level refusals answer with `detail` as a string; pydantic's answer with a list.
         That is the only distinction needed here, and no server text is shown to the reader. */
      var refusal = null;
      try { refusal = (await response.clone().json()).detail; } catch (error) { refusal = null; }
      if (typeof refusal === 'string' && refusal.indexOf('push service') !== -1) {
        finish('Dein Browser nutzt einen Push-Dienst, den wir noch nicht unterstützen. Das liegt '
          + 'nicht an dir. Bitte melde uns, welchen Browser du benutzt – oder nimm so lange einen '
          + 'anderen Browser.');
      } else {
        finish('Das hat nicht geklappt. Bitte prüfe die Eingaben und versuch es noch einmal.');
      }
    } else {
      finish('Bei uns ist gerade etwas schiefgegangen. Bitte später noch einmal probieren.');
    }
    return;
  }
  if (channel === 'email') {
    out.textContent = 'Fast fertig – schau in dein Postfach und bestätige die Anmeldung.';
    settled();
    return;
  }

  // Push: the browser already holds the subscription it gave us a moment ago, and the test
  // notification is on its way to it. There is nothing to display and nothing to copy - which is
  // the whole of what changed with D-45. Under ntfy this is where the topic, the QR code, the two
  // subscribe links and a three-step explainer lived, because none of it could be skipped.
  //
  // What is left is a wait. It is a real wait: the push service has to reach the device, which is
  // usually under a second and occasionally several, so saying so beats an empty pause.
  /* Guarded for the same reason as the fetch above: a truncated or non-JSON body rejects here, and
     everything after this line - including `settled()` - would never run, leaving the button
     disabled on a signup the server has in fact accepted. Falling back to an empty object takes the
     normal path, which is the right guess: a 2xx means the subscription exists. */
  var data;
  try {
    data = await response.json();
  } catch (error) {
    data = {};
  }
  if (data.already_active) {
    /* This browser was already subscribed, so no test notification is on its way and a spinner
       waiting for one would never resolve. The location has been updated, which is what signing up
       again from this page almost always means - somebody moved, or picked a better spot. */
    out.innerHTML = '';
    out.appendChild(el('p', 'done', 'Dieser Browser war schon angemeldet \u2013 der Ort ist jetzt aktualisiert.'));
    var toSettings = el('p', 'alt');
    var link = document.createElement('a');
    link.href = '/manage';
    link.textContent = 'Einstellungen \u00f6ffnen';
    toSettings.appendChild(link);
    out.appendChild(toSettings);
    settled(false);
    return;
  }

  out.innerHTML = '';
  var waiting = el('p', 'waiting');
  waiting.appendChild(el('span', 'spinner'));
  waiting.appendChild(el('span', null, 'Wir schicken dir gerade eine Benachrichtigung zum Best\u00e4tigen \u2013 tippe sie an, dann bist du angemeldet.'));
  out.appendChild(waiting);
  // No auto-refresh and no polling. Confirming happens on the device, in the notification, and
  // this page has no way to learn that it happened - the confirm link opens /confirm, which is
  // where the reader ends up. A "waiting..." that never resolves is honest; a spinner that spins
  // forever after a successful confirmation elsewhere would not be, so it says what to do rather
  // than promising to update itself.
  // Ends at the browser settings, not at "melde dich noch einmal an": `settled()` hides the form
  // on the next line, so that instruction named a control no longer on the page - and the reader
  // whose notification did not arrive is exactly the one who scrolls looking for it. The restart
  // affordance is `settled()`'s own "Von vorn anfangen" link, and there is now only one of them.
  out.appendChild(el('p', 'alt', 'Kommt nichts an? Dann erreichen dich die Warnungen auch nicht. '
    + 'Pr\u00fcfe die Benachrichtigungen f\u00fcr diese Seite in den Browser-Einstellungen und fang '
    + 'dann von vorn an.'));
  settled();
});
