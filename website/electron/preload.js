const { contextBridge, ipcRenderer } = require("electron");

// ── Reading main-process values in a SANDBOXED preload ──
//
// This preload runs sandboxed: `webPreferences` sets `nodeIntegration: false` and
// never sets `sandbox`, and since Electron 20 that means the sandbox is on. A
// sandboxed preload's `require` is a polyfill limited to `electron`, `events`,
// `timers` and `url` — a relative `require("./display-media")` would throw
// "module not found" and take THIS ENTIRE FILE down with it, so `window.kirocrew`,
// `electronAPI`, `zoomAPI` and `updateAPI` would all vanish from the renderer.
//
// So values computed in main.js arrive as `additionalArguments`, which Electron
// appends to the renderer's `process.argv` for exactly this purpose. `process` is
// one of the globals the sandboxed preload does polyfill.
const argvValue = (flag, fallback) => {
  const prefix = `--${flag}=`;
  const hit = (process.argv || []).find((a) => typeof a === "string" && a.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : fallback;
};

contextBridge.exposeInMainWorld("kirocrew", {
  platform: process.platform,
  isElectron: true,
  // Which audio-capture tier this desktop build gets, so the meeting UI can tell
  // the user what to expect BEFORE they click Record rather than explaining a
  // failure afterwards. main.js derives it with `display-media.js`'s
  // `describeAudioTier` — the same module that decides the ACTUAL grant, so the
  // guidance and the behaviour cannot drift apart.
  //
  // A plain browser has no `window.kirocrew` at all, which is the "browser" tier;
  // the renderer treats an unrecognised value the same way (see captureTier.ts).
  audioTier: argvValue("kirocrew-audio-tier", ""),
});

contextBridge.exposeInMainWorld("electronAPI", {
  onStatus: (cb) => {
    const handler = (_e, msg) => cb(msg);
    ipcRenderer.on("status", handler);
    return () => ipcRenderer.removeListener("status", handler);
  },
  // Boot-reveal handshake: main.js sends "boot-ready" once the gateway is up;
  // loading.html replies "boot-complete" after its reveal animation fades out.
  onBootReady: (cb) => {
    const handler = () => cb();
    ipcRenderer.on("boot-ready", handler);
    return () => ipcRenderer.removeListener("boot-ready", handler);
  },
  bootComplete: () => ipcRenderer.send("boot-complete"),
  // Persist the user's resolved theme accent (a hex string) so the next launch's
  // boot splash (loading.html) can paint in the user's chosen colour. Read back
  // by main.js and injected as a query param — see showLoadingThenConnect.
  setThemeAccent: (hex) => ipcRenderer.send("theme-accent-changed", String(hex || "")),
  // Dev mode IPC: renderer signals main process to show/hide DevTools menu item.
  setDevMode: (enabled) => ipcRenderer.send("dev-mode-changed", !!enabled),
  // App-menu navigation: main.js sends an in-app path ("/settings",
  // "/settings?tab=about") when the user picks Settings…/About from the
  // native application menu; the SPA routes to it (see App.tsx).
  onNavigate: (cb) => {
    const handler = (_e, path) => cb(path);
    ipcRenderer.on("navigate", handler);
    return () => ipcRenderer.removeListener("navigate", handler);
  },
  onFullScreenChanged: (callback) => {
    const handler = (_event, isFullScreen) => callback(!!isFullScreen);
    ipcRenderer.on("fullscreen-changed", handler);
    return () => ipcRenderer.removeListener("fullscreen-changed", handler);
  },
  // Dock/taskbar badge (RFC notification bus Phase 4): the renderer pushes
  // its unread (critical+default) count; main.js applies app.setBadgeCount.
  // No-op on platforms without badge support (Windows) -- Electron handles it.
  setBadgeCount: (count) => ipcRenderer.send("badge:set", count),
  // Mic-denial recovery. The renderer only ever sees getUserMedia's
  // NotAllowedError — it cannot tell "the OS refused" from "Electron refused",
  // and it cannot open System Settings itself. So when a mic capture is denied
  // it pings the main process, which checks the real OS status and shows the
  // Privacy-pane dialog only if macOS is actually the one saying no. Without
  // this the toast is a dead end: macOS never re-prompts after a denial.
  reportMicDenied: () => ipcRenderer.send("mic:denied"),
});

// Native zoom bridge for the Settings > Display "Zoom Level" stepper.
// Chromium's per-origin zoom (the thing Cmd/Ctrl +/- changes) is not
// reachable from page JS, so the renderer round-trips through main.js.
// All three calls resolve with the applied zoom factor. Absent in plain
// browsers — the renderer treats a missing bridge as "zoom not controllable"
// and shows a shortcut hint instead of the stepper.
contextBridge.exposeInMainWorld("zoomAPI", {
  get: () => ipcRenderer.invoke("zoom:get"),
  set: (factor) => ipcRenderer.invoke("zoom:set", factor),
  step: (dir) => ipcRenderer.invoke("zoom:step", dir),
});

// Desktop auto-update bridge. Drives the in-app UpdateModal + Settings > About.
// onState pushes update lifecycle events ({state, version, notes, channel});
// check/install/getInfo are promise-based round-trips to the main process.
contextBridge.exposeInMainWorld("updateAPI", {
  onState: (cb) => {
    const handler = (_e, payload) => cb(payload);
    ipcRenderer.on("update-state", handler);
    return () => ipcRenderer.removeListener("update-state", handler);
  },
  check: () => ipcRenderer.invoke("update:check"),
  download: () => ipcRenderer.invoke("update:download"),
  install: () => ipcRenderer.invoke("update:install"),
  getInfo: () => ipcRenderer.invoke("update:get-info"),
  // Channel switcher (Settings > About): "" follows the build stamp,
  // "insider"|"stable" opts the production app onto that lane.
  setChannel: (channel) => ipcRenderer.invoke("update:set-channel", channel),
});
