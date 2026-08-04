// Screen-share request handling for the KiroCrew Electron app.
//
// Why this exists: in a browser, navigator.mediaDevices.getDisplayMedia() shows
// the OS picker natively. In Electron (>= 20) the renderer's call is REJECTED
// unless the main process registers session.setDisplayMediaRequestHandler().
// Without it, the chat input's screen-snip tool silently does nothing in the
// packaged app (the renderer's capture promise rejects and the snip code treats
// it as "user cancelled"). This module supplies that handler.
//
// The Electron runtime glue (session, desktopCapturer, systemPreferences) is
// injected so the selection + permission logic is unit-testable without a live
// Electron process — mirroring how the renderer keeps getDisplayMedia at an
// untested I/O boundary.
"use strict";

/**
 * Pick the best capture source from desktopCapturer.getSources().
 * Prefers a whole-screen source ("screen:*") over a window, since the snip
 * tool crops a region out of a full frame. Falls back to the first source.
 *
 * @param {Array<{id: string, name: string}>} sources
 * @returns {object|null} the chosen source, or null if there are none
 */
function chooseDisplaySource(sources) {
  if (!Array.isArray(sources) || sources.length === 0) return null;
  const screen = sources.find((s) => typeof s.id === "string" && s.id.startsWith("screen:"));
  return screen || sources[0];
}

/**
 * Build the handler passed to session.setDisplayMediaRequestHandler().
 *
 * @param {object} deps
 * @param {() => Promise<Array>} deps.getSources - desktopCapturer.getSources wrapper (REQUIRED)
 * @param {() => string} [deps.getScreenAccessStatus] - systemPreferences.getMediaAccessStatus('screen')
 * @param {(reason: string) => void} [deps.onPermissionNeeded] - surfaced when capture is blocked
 *        (reason: 'denied' = macOS Screen Recording off; 'no-sources' = nothing capturable)
 * @param {string} [deps.platform] - process.platform (defaults to the running platform)
 * @returns {(request: object, callback: (streams: object) => void) => Promise<void>}
 */
/**
 * Platforms where Electron's `Streams.audio: 'loopback'` actually captures system
 * audio.
 *
 * Windows only, and that is Electron's own statement, not a guess — see the
 * `Streams.audio` doc comment in `electron.d.ts` for the version in
 * `node_modules` (43.2.0): a loopback device "is currently only supported on
 * Windows". Granting it elsewhere would be asking for a device the platform
 * cannot provide, on a handler the chat screen-snip tool also depends on.
 *
 * macOS does not need this path: `setDisplayMediaRequestHandler` is installed
 * with `{ useSystemPicker: true }`, and when the native picker is available
 * Electron uses it and DOES NOT invoke this handler at all — the OS picker is
 * what offers (or withholds) audio there.
 *
 * Deliberately NOT `electron-audio-loopback`. That package documents itself as
 * required for Electron >= 31 and < 39, and states that from Electron 39 on it is
 * unnecessary; this app is on 43.2.0, so adding it would be a dependency its own
 * author says is not needed here.
 */
const LOOPBACK_AUDIO_PLATFORMS = new Set(["win32"]);

/**
 * Whether the native macOS screen picker is preferred over this handler.
 *
 * Lives here rather than inline at the `setDisplayMediaRequestHandler` call so
 * main.js and preload.js read ONE value. The renderer's audio-tier guidance is
 * derived from it (see `describeAudioTier`), and a copy that drifted from the flag
 * actually passed to Electron would tell the user to expect the wrong thing.
 */
const USE_SYSTEM_PICKER = true;

/**
 * Decide what to put in `Streams.audio`, or `undefined` for "grant no audio".
 *
 * Two gates, both load-bearing:
 *
 * 1. `audioRequested` — this handler is SHARED with the chat input's screen-snip
 *    tool, which asks for video only. Attaching a loopback audio device to a snip
 *    would start capturing the user's system audio for a screenshot.
 * 2. platform — see LOOPBACK_AUDIO_PLATFORMS.
 *
 * @param {{audioRequested?: boolean, platform?: string}} opts
 * @returns {"loopback"|undefined}
 */
function chooseAudioGrant(opts) {
  const { audioRequested, platform } = opts || {};
  if (!audioRequested) return undefined;
  if (!LOOPBACK_AUDIO_PLATFORMS.has(platform)) return undefined;
  return "loopback";
}

function createDisplayMediaHandler(deps) {
  if (!deps || typeof deps.getSources !== "function") {
    throw new Error("getSources is required");
  }
  const getSources = deps.getSources;
  const getScreenAccessStatus = deps.getScreenAccessStatus || (() => "granted");
  const onPermissionNeeded = deps.onPermissionNeeded || (() => {});
  const platform = deps.platform || process.platform;

  return async function handleDisplayMediaRequest(request, callback) {
    try {
      // macOS gates screen capture behind the Screen Recording TCC permission.
      // 'not-determined' is allowed through — getSources() triggers the OS
      // prompt. An explicit 'denied'/'restricted' will never yield frames, so
      // short-circuit and guide the user to System Settings instead of failing
      // opaquely.
      if (platform === "darwin") {
        const status = getScreenAccessStatus();
        if (status === "denied" || status === "restricted") {
          onPermissionNeeded("denied");
          callback({}); // deny -> renderer getDisplayMedia rejects -> snip no-ops cleanly
          return;
        }
      }

      const sources = await getSources();
      const source = chooseDisplaySource(sources);
      if (!source) {
        onPermissionNeeded("no-sources");
        callback({});
        return;
      }
      // Meeting capture asks for audio as well as video; the snip tool does not.
      // Where Electron can supply a loopback device, granting it here means the
      // meeting gets the other participants' voices with NO picker at all, since
      // this handler already auto-selects the source.
      const streams = { video: source };
      const audio = chooseAudioGrant({
        audioRequested: request && request.audioRequested,
        platform,
      });
      if (audio) streams.audio = audio;
      callback(streams);
    } catch (err) {
      // Never throw out of the handler: a rejection here would crash the
      // request. Deny gracefully so the renderer's catch path runs.
      onPermissionNeeded("error");
      callback({});
    }
  };
}

/**
 * Which audio-capture tier this platform gets, as a string the renderer can
 * branch its guidance on.
 *
 * Derived from the same two facts the handler uses, so the message the user reads
 * and the grant they actually get cannot disagree:
 *
 * - `loopback`      — this handler grants a loopback device, no picker, system
 *                     audio just arrives (Windows).
 * - `system-picker`  — Electron uses the native macOS picker instead of this
 *                     handler, so whether audio arrives is the user's pick.
 * - `video-only`     — this handler runs but cannot grant audio, and it
 *                     auto-selects a source, so there is no picker in which the
 *                     user could offer audio either. Microphone only (Linux).
 *
 * @param {{platform?: string, useSystemPicker?: boolean}} opts
 * @returns {"loopback"|"system-picker"|"video-only"}
 */
function describeAudioTier(opts) {
  const { platform, useSystemPicker } = opts || {};
  if (LOOPBACK_AUDIO_PLATFORMS.has(platform)) return "loopback";
  // `useSystemPicker` is macOS-only in Electron; elsewhere the flag is inert, so
  // the platform has to agree before the picker can be credited for audio.
  if (useSystemPicker && platform === "darwin") return "system-picker";
  return "video-only";
}

module.exports = {
  chooseDisplaySource,
  chooseAudioGrant,
  createDisplayMediaHandler,
  describeAudioTier,
  LOOPBACK_AUDIO_PLATFORMS,
  USE_SYSTEM_PICKER,
};
