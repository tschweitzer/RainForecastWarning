/* Browser geolocation, and the four ways it declines to work.
 *
 * The button this replaces called getCurrentPosition with a success callback and nothing else.
 * That is silent failure by construction: every way the API says no arrives on the *error*
 * callback, so a button with no error callback does nothing, says nothing, and looks broken.
 *
 * The one worth naming separately is the secure-context rule. Over plain http on anything but
 * localhost, `navigator.geolocation` still exists - so a `if (!navigator.geolocation)` guard
 * passes - and the call then fails with PERMISSION_DENIED and "Only secure origins are allowed".
 * No page code can fix that. Saying so is the only useful response, because the remedy is an
 * https origin or an ssh tunnel to localhost, not another click.
 */
(function (global) {
  'use strict';

  // Germany plus the margin the API enforces (subscriptions.LAT_RANGE / LON_RANGE). Checked here
  // as well so someone abroad is told why, rather than having a coordinate filled in for them
  // that the server then refuses.
  var LAT = [47.0, 56.0];
  var LON = [5.0, 16.0];

  var MESSAGES = {
    insecure: 'Die Standortbestimmung braucht eine sichere Verbindung. Über http erlauben '
      + 'Browser sie nur auf localhost – bitte die Koordinaten eintippen oder die Seite über '
      + 'https aufrufen.',
    unsupported: 'Dieser Browser kennt keine Standortbestimmung – bitte die Koordinaten eintippen.',
    denied: 'Der Browser hat den Zugriff auf den Standort abgelehnt. Du kannst ihn in den '
      + 'Einstellungen dieser Seite wieder erlauben.',
    unavailable: 'Der Standort war gerade nicht zu ermitteln. Bitte noch einmal versuchen.',
    timeout: 'Die Standortbestimmung hat zu lange gedauert. Bitte noch einmal versuchen.',
    outside: 'Dieser Standort liegt außerhalb des Gebiets, das der DWD-Radarverbund abdeckt – '
      + 'der Dienst warnt zurzeit nur in Deutschland.',
    busy: 'Standort wird bestimmt …'
  };

  function describe(error) {
    // Chrome reports the secure-origin refusal as PERMISSION_DENIED, which would otherwise be
    // shown as "you declined" to someone who was never asked.
    if (!global.isSecureContext) { return MESSAGES.insecure; }
    if (error.code === 1) { return MESSAGES.denied; }
    if (error.code === 2) { return MESSAGES.unavailable; }
    if (error.code === 3) { return MESSAGES.timeout; }
    return MESSAGES.unavailable;
  }

  /**
   * locate({onFound, onStatus, onBusy})
   *
   * onFound(lat, lon, accuracyMetres) - a usable position inside the covered area.
   * onStatus(text, kind)              - something to tell the person; kind is 'error' or 'info'.
   * onBusy(isBusy)                    - optional, for disabling a button while it runs.
   */
  function locate(handlers) {
    var onFound = handlers.onFound;
    var onStatus = handlers.onStatus || function () {};
    var onBusy = handlers.onBusy || function () {};

    if (!global.isSecureContext) { onStatus(MESSAGES.insecure, 'error'); return; }
    if (!global.navigator || !global.navigator.geolocation) {
      onStatus(MESSAGES.unsupported, 'error');
      return;
    }

    onBusy(true);
    onStatus(MESSAGES.busy, 'info');
    global.navigator.geolocation.getCurrentPosition(
      function (position) {
        onBusy(false);
        var lat = position.coords.latitude;
        var lon = position.coords.longitude;
        if (lat < LAT[0] || lat > LAT[1] || lon < LON[0] || lon > LON[1]) {
          onStatus(MESSAGES.outside, 'error');
          return;
        }
        onStatus('', 'info');
        onFound(lat, lon, position.coords.accuracy);
      },
      function (error) {
        onBusy(false);
        onStatus(describe(error), 'error');
      },
      // A timeout is not optional: without one the callback can simply never arrive - a headless
      // browser with no location provider does exactly that - and the button stays "busy"
      // forever, which is the silent failure again in a different costume.
      {enableHighAccuracy: true, timeout: 10000, maximumAge: 60000}
    );
  }

  global.RainGeo = {locate: locate, messages: MESSAGES};
})(window);
