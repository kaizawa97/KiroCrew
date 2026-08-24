const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  chooseAudioGrant,
  chooseDisplaySource,
  createDisplayMediaHandler,
  describeAudioTier,
} = require("../display-media");

describe("chooseDisplaySource", () => {
  it("returns null when there are no sources", () => {
    assert.equal(chooseDisplaySource([]), null);
    assert.equal(chooseDisplaySource(undefined), null);
  });

  it("prefers a whole-screen source over a window source", () => {
    const win = { id: "window:42:0", name: "Some App" };
    const screen = { id: "screen:1:0", name: "Entire Screen" };
    assert.equal(chooseDisplaySource([win, screen]), screen);
  });

  it("returns the first source when no screen sources are present", () => {
    const a = { id: "window:1:0", name: "A" };
    const b = { id: "window:2:0", name: "B" };
    assert.equal(chooseDisplaySource([a, b]), a);
  });
});

describe("createDisplayMediaHandler", () => {
  const screenSrc = { id: "screen:1:0", name: "Entire Screen" };

  it("grants the chosen source via callback when sources are available", async () => {
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      getScreenAccessStatus: () => "granted",
      platform: "darwin",
    });
    await handler({}, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });

  it("denies and notifies on macOS when screen access is denied (without calling getSources)", async () => {
    let calledGetSources = false;
    let reason;
    let streams = "untouched";
    const handler = createDisplayMediaHandler({
      getSources: async () => {
        calledGetSources = true;
        return [screenSrc];
      },
      getScreenAccessStatus: () => "denied",
      onPermissionNeeded: (r) => {
        reason = r;
      },
      platform: "darwin",
    });
    await handler({}, (s) => {
      streams = s;
    });
    assert.equal(calledGetSources, false);
    assert.equal(reason, "denied");
    assert.deepEqual(streams, {});
  });

  it("denies and notifies when no capture sources are returned", async () => {
    let reason;
    let streams = "untouched";
    const handler = createDisplayMediaHandler({
      getSources: async () => [],
      getScreenAccessStatus: () => "granted",
      onPermissionNeeded: (r) => {
        reason = r;
      },
      platform: "darwin",
    });
    await handler({}, (s) => {
      streams = s;
    });
    assert.equal(reason, "no-sources");
    assert.deepEqual(streams, {});
  });

  it("denies gracefully (no throw) when getSources rejects", async () => {
    let streams = "untouched";
    const handler = createDisplayMediaHandler({
      getSources: async () => {
        throw new Error("desktopCapturer failed");
      },
      getScreenAccessStatus: () => "granted",
      platform: "darwin",
    });
    await handler({}, (s) => {
      streams = s;
    });
    assert.deepEqual(streams, {});
  });

  it("ignores screen-access status on non-darwin platforms and proceeds", async () => {
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      // even if this said 'denied', linux must not short-circuit
      getScreenAccessStatus: () => "denied",
      platform: "linux",
    });
    await handler({}, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });

  it("grants a loopback audio device on Windows when audio was requested", async () => {
    // The meeting-capture win: the handler auto-selects the source, so on Windows
    // the other participants' audio arrives with no picker at all.
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      platform: "win32",
    });
    await handler({ audioRequested: true }, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc, audio: "loopback" });
  });

  it("does NOT attach audio to a video-only request", async () => {
    // This handler is shared with the chat input's screen-snip tool, which asks for
    // video only. Attaching a loopback device there would start capturing the
    // user's system audio to take a screenshot.
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      platform: "win32",
    });
    await handler({ audioRequested: false }, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });

  it("treats a request with no audioRequested field as video-only", async () => {
    // Every existing caller (and every existing test) passes a bare {}.
    let granted;
    const handler = createDisplayMediaHandler({
      getSources: async () => [screenSrc],
      platform: "win32",
    });
    await handler({}, (streams) => {
      granted = streams;
    });
    assert.deepEqual(granted, { video: screenSrc });
  });

  it("does not offer loopback audio where Electron cannot supply it", async () => {
    // electron.d.ts (43.2.0) states a loopback device is currently Windows-only.
    for (const platform of ["darwin", "linux"]) {
      let granted;
      const handler = createDisplayMediaHandler({
        getSources: async () => [screenSrc],
        getScreenAccessStatus: () => "granted",
        platform,
      });
      await handler({ audioRequested: true }, (streams) => {
        granted = streams;
      });
      assert.deepEqual(granted, { video: screenSrc }, platform);
    }
  });
});

describe("chooseAudioGrant", () => {
  it("requires BOTH an audio request and a supporting platform", () => {
    assert.equal(chooseAudioGrant({ audioRequested: true, platform: "win32" }), "loopback");
    assert.equal(chooseAudioGrant({ audioRequested: false, platform: "win32" }), undefined);
    assert.equal(chooseAudioGrant({ audioRequested: true, platform: "darwin" }), undefined);
    assert.equal(chooseAudioGrant({ audioRequested: true, platform: "linux" }), undefined);
  });

  it("tolerates a missing or empty options object", () => {
    assert.equal(chooseAudioGrant(), undefined);
    assert.equal(chooseAudioGrant({}), undefined);
  });
});

describe("describeAudioTier", () => {
  it("reports loopback where the handler grants a device itself", () => {
    assert.equal(describeAudioTier({ platform: "win32", useSystemPicker: true }), "loopback");
    // The flag is irrelevant on Windows — the handler is what grants audio there.
    assert.equal(describeAudioTier({ platform: "win32", useSystemPicker: false }), "loopback");
  });

  it("credits the native picker only on macOS, where Electron actually has one", () => {
    assert.equal(describeAudioTier({ platform: "darwin", useSystemPicker: true }), "system-picker");
    // Without the flag the handler runs on macOS too, and it cannot grant audio.
    assert.equal(describeAudioTier({ platform: "darwin", useSystemPicker: false }), "video-only");
    // useSystemPicker is macOS-only in Electron, so it must not be credited elsewhere.
    assert.equal(describeAudioTier({ platform: "linux", useSystemPicker: true }), "video-only");
  });

  it("falls back to video-only for an unknown platform", () => {
    assert.equal(describeAudioTier({ platform: "freebsd" }), "video-only");
    assert.equal(describeAudioTier(), "video-only");
  });
});
