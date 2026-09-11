"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const path = require("node:path");
const { describe, it } = require("node:test");
const {
  fetchLocalToken,
  literalLoopbackUrl,
  decideLocalMint,
} = require("../local-token");

// A fetchLocalToken harness that records every fact a leak would need: which
// secrets were read from disk and which requests were issued with them, plus
// which locality questions were asked and in what order.
function harness({
  backendUrl = "http://localhost:5476",
  remoteHost = "",
  portOwner = "kirocrew",
  status = 200,
  body = JSON.stringify({ token: "minted-token" }),
  secret = "canonical-secret",
  omitLocality = false,
} = {}) {
  const reads = [];
  const sentSecrets = [];
  const requestedUrls = [];
  const asked = [];
  const logs = [];

  const deps = {
    backendUrl,
    resolveHome: () => path.resolve(path.sep, "canonical"),
    path,
    fs: {
      readFileSync(secretPath) {
        reads.push(secretPath);
        return secret;
      },
    },
    http: {
      get(url, options, callback) {
        requestedUrls.push(url);
        sentSecrets.push(options.headers["X-Local-Secret"]);
        const request = new EventEmitter();
        request.destroy = () => {};
        const response = new EventEmitter();
        response.statusCode = status;
        response.resume = () => {};
        queueMicrotask(() => {
          callback(response);
          response.emit("data", body);
          response.emit("end");
        });
        return request;
      },
    },
    log: (line) => logs.push(line),
  };
  if (!omitLocality) {
    deps.getRemoteHost = (port) => {
      asked.push(`remote-host:${port}`);
      return typeof remoteHost === "function" ? remoteHost(port) : remoteHost;
    };
    deps.getPortOwner = (port) => {
      asked.push(`port-owner:${port}`);
      return typeof portOwner === "function" ? portOwner(port) : portOwner;
    };
  }

  return {
    reads,
    sentSecrets,
    requestedUrls,
    asked,
    logs,
    run: () => fetchLocalToken(deps),
  };
}

describe("fetchLocalToken", () => {
  it("sends only the call-time authoritative secret to literal IPv4 loopback", async () => {
    // Keys are built with path.join, matching how fetchLocalToken composes the
    // secret path: a POSIX literal would never match the backslash-separated
    // path the real path.join produces on Windows, so the fake fs would return
    // undefined and the test would fail on path syntax rather than on the
    // call-time-authoritative-secret rule it exists to pin.
    const CANONICAL_HOME = path.resolve(path.sep, "canonical");
    const LEGACY_HOME = path.resolve(path.sep, "legacy");
    const files = new Map([
      [path.join(CANONICAL_HOME, ".local_secret"), "canonical-secret"],
      [path.join(LEGACY_HOME, ".local_secret"), "legacy-secret"],
    ]);
    const attempted = [];
    const requestedUrls = [];
    const fakeFs = {
      readFileSync(path) { return files.get(path); },
    };
    const fakeHttp = {
      get(url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        const secret = options.headers["X-Local-Secret"];
        attempted.push(secret);
        requestedUrls.push(url);
        const response = new EventEmitter();
        response.statusCode = 200;
        response.resume = () => {};
        queueMicrotask(() => {
          callback(response);
          response.emit("data", JSON.stringify({ token: "migrated-token" }));
          response.emit("end");
        });
        return request;
      },
    };

    const token = await fetchLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => LEGACY_HOME,
      path,
      fs: fakeFs,
      http: fakeHttp,
      getRemoteHost: () => "",
      getPortOwner: () => "kirocrew",
    });

    assert.equal(token, "migrated-token");
    assert.deepEqual(attempted, ["legacy-secret"]);
    assert.deepEqual(requestedUrls, ["http://127.0.0.1:5476/api/token/local"]);
  });

  it("does not fall back to another home when the authoritative secret is rejected", async () => {
    const attempted = [];
    const fakeFs = {
      readFileSync(secretPath) {
        assert.equal(secretPath, path.join("/canonical", ".local_secret"));
        return "canonical-secret";
      },
    };
    const fakeHttp = {
      get(_url, options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        attempted.push(options.headers["X-Local-Secret"]);
        const response = new EventEmitter();
        response.statusCode = 403;
        response.resume = () => {};
        queueMicrotask(() => callback(response));
        return request;
      },
    };

    const token = await fetchLocalToken({
      backendUrl: "http://localhost:5476",
      resolveHome: () => "/canonical",
      path,
      fs: fakeFs,
      http: fakeHttp,
      getRemoteHost: () => "",
      getPortOwner: () => "kirocrew",
    });

    assert.equal(token, "");
    assert.deepEqual(attempted, ["canonical-secret"]);
  });

  it("refuses to send a local secret to a non-literal remote address", async () => {
    let called = false;
    const token = await fetchLocalToken({
      backendUrl: "http://example.com:5476",
      resolveHome: () => "/canonical",
      path,
      fs: { readFileSync: () => "canonical-secret" },
      http: { get: () => { called = true; } },
      getRemoteHost: () => "",
      getPortOwner: () => "kirocrew",
    });

    assert.equal(token, "");
    assert.equal(called, false);
  });

  // The leak this file exists to pin: a gateway on another host reached through
  // `ssh -L 5476:localhost:5476 host` is addressed as http://localhost:5476, so
  // it passes every loopback test while the request travels down the forward.
  it("never reads or sends the secret to a port with a remote host configured", async () => {
    const h = harness({ remoteHost: "hostile.example.com" });

    assert.equal(await h.run(), "");
    assert.deepEqual(h.reads, [], "the secret must not even be read from disk");
    assert.deepEqual(h.sentSecrets, []);
    assert.deepEqual(h.requestedUrls, []);
    // Configuration first: the credential source is decided from what the user
    // told us, not by trying the local mint and reading the answer.
    assert.deepEqual(h.asked, ["remote-host:5476"]);
    assert.match(h.logs.join("\n"), /remote-configured/);
  });

  it("asks about the window's OWN port, including a scheme-default one", async () => {
    // URL.port is "" on http://localhost/, which would consult remoteHosts[""]
    // and probe no port at all — i.e. clear a tunnel it never looked at.
    const h = harness({
      backendUrl: "http://localhost/",
      remoteHost: (port) => (port === "80" ? "hostile.example.com" : ""),
    });

    assert.equal(await h.run(), "");
    assert.deepEqual(h.asked, ["remote-host:80"]);
    assert.deepEqual(h.reads, []);
    assert.deepEqual(h.sentSecrets, []);
  });

  // A manual `ssh -L` forward appears in no remote-host config, so only positive
  // listener ownership can tell it apart from this machine's own gateway.
  for (const owner of ["foreign", "none", "unknown", "", "kirocrew-ish"]) {
    it(`refuses a listener classified ${JSON.stringify(owner)}`, async () => {
      const h = harness({ portOwner: owner });

      assert.equal(await h.run(), "");
      assert.deepEqual(h.reads, []);
      assert.deepEqual(h.requestedUrls, []);
      assert.deepEqual(h.asked, ["remote-host:5476", "port-owner:5476"]);
    });
  }

  it("refuses when the listener probe throws", async () => {
    const h = harness({
      portOwner: () => { throw new Error("lsof missing"); },
    });

    assert.equal(await h.run(), "");
    assert.deepEqual(h.reads, []);
    assert.deepEqual(h.requestedUrls, []);
  });

  it("refuses when the remote-host config cannot be read", async () => {
    const h = harness({
      remoteHost: () => { throw new Error("store unreadable"); },
    });

    assert.equal(await h.run(), "");
    assert.deepEqual(h.reads, []);
    assert.deepEqual(h.asked, ["remote-host:5476"]);
  });

  it("mints for this shell's own gateway, service-managed or not", async () => {
    for (const owner of ["kirocrew", "service"]) {
      const h = harness({ portOwner: owner });

      assert.equal(await h.run(), "minted-token", owner);
      assert.deepEqual(h.sentSecrets, ["canonical-secret"], owner);
      assert.deepEqual(
        h.requestedUrls,
        ["http://127.0.0.1:5476/api/token/local"],
        owner,
      );
    }
  });

  it("fails closed when a caller supplies no locality evidence", async () => {
    const h = harness({ omitLocality: true });

    assert.equal(await h.run(), "");
    assert.deepEqual(h.reads, []);
    assert.deepEqual(h.requestedUrls, []);
    assert.match(h.logs.join("\n"), /no-locality-evidence/);
  });
});

describe("decideLocalMint", () => {
  it("returns no mint URL on any refusal", async () => {
    const refusals = [
      { backendUrl: "http://example.com:5476", getRemoteHost: () => "", getPortOwner: () => "kirocrew" },
      { backendUrl: "http://localhost:5476", getRemoteHost: () => "host", getPortOwner: () => "kirocrew" },
      { backendUrl: "http://localhost:5476", getRemoteHost: () => "", getPortOwner: () => "foreign" },
      { backendUrl: "http://localhost:5476" },
    ];
    for (const input of refusals) {
      const verdict = await decideLocalMint(input);
      assert.equal(verdict.ok, false, JSON.stringify(input.backendUrl));
      assert.equal(verdict.url, "");
    }
  });

  it("hands back the literal loopback origin for an own gateway", async () => {
    const verdict = await decideLocalMint({
      backendUrl: "http://kirocrew.localhost:7778/chat?new=1",
      getRemoteHost: () => "",
      getPortOwner: () => "service",
    });

    assert.deepEqual(verdict, {
      ok: true,
      reason: "listener-service",
      url: "http://127.0.0.1:7778",
      port: "7778",
    });
  });
});

describe("literalLoopbackUrl", () => {
  it("preserves the port while replacing hostname aliases", () => {
    assert.equal(literalLoopbackUrl("http://localhost:6777"), "http://127.0.0.1:6777");
    assert.equal(literalLoopbackUrl("http://kirocrew.localhost:6777"), "http://127.0.0.1:6777");
  });
});
