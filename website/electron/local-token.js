"use strict";
//
// local-token.js — minting a dashboard token from THIS machine's .local_secret.
//
// `.local_secret` is the LOCAL gateway's owner credential: /api/token/local
// exchanges it for a full owner token, and the token-auth middleware accepts the
// same value as X-Internal-Secret on internal routes. Whoever receives it owns
// the local gateway for its uptime, so this module answers ONE question before
// the secret is read from disk: is the listener on the target port positively
// this machine's own gateway?
//
// Loopback is necessary but nowhere near sufficient. Remote Tunnel Mode reaches
// a gateway on another host through `ssh -L <port>:localhost:<remotePort> host`
// and addresses it as `http://localhost:<port>`, so a tunnelled remote passes
// every hostname test while the request — and the header on it — travels down
// the forward to that host. Presenting the secret there buys nothing (a foreign
// gateway signs with its OWN secret and 403s ours, which token-acquire.js
// already documents) and costs everything (a hostile remote gains an owner
// session on this machine's gateway).
//
// So the mint is decided from evidence, not by trial — see decideLocalMint:
// configured-remote FIRST (the user already told us the gateway is elsewhere),
// then positive listener ownership, the same locality/ownership pair the
// three-gate IPC channels and isGatewayLocalForWindow use. Every call site
// (boot connect, 403 retry, renderer recovery, Refresh Token, the companion
// surfaces) reaches the secret through fetchLocalToken, so the gate lives here
// rather than at the call sites: a new caller inherits the refusal instead of
// inheriting the disclosure.

const { defaultedPort } = require("./gateway-auth-hint");

// LISTEN-socket owners that positively identify this shell's own gateway (see
// classifyPortOwner). "foreign" (an `ssh -L` client relaying to another host, an
// unrelated process), "none" (nothing bound locally, so whatever answered did so
// through a forward) and "unknown" (the probe could not run) all fail CLOSED:
// the cost of refusing is a token prompt, the cost of guessing is handing this
// machine's owner credential to another host.
const OWN_GATEWAY_OWNERS = new Set(["kirocrew", "service"]);

function literalLoopbackUrl(backendUrl) {
  try {
    const url = new URL(backendUrl);
    if (url.protocol !== "http:") return "";
    if (url.hostname === "localhost" || url.hostname === "kirocrew.localhost") {
      url.hostname = "127.0.0.1";
    }
    if (url.hostname !== "127.0.0.1") return "";
    return url.origin;
  } catch {
    return "";
  }
}

/**
 * May this shell present its own `.local_secret` to the gateway at `backendUrl`?
 *
 * Pure policy over injected facts, so the decision is testable without a
 * network, a filesystem or an Electron runtime.
 *
 * @param {object} o
 * @param {string} o.backendUrl  the window's gateway URL.
 * @param {(port: string) => (string|Promise<string>)} o.getRemoteHost
 *        remote host configured for that port, "" when none. Consulted FIRST:
 *        a configured host means SSH is the only credential source, so the
 *        secret is never read, let alone sent, for that port.
 * @param {(port: string) => (string|Promise<string>)} o.getPortOwner
 *        classifyPortOwner verdict for that port. Only "kirocrew"/"service"
 *        permit the mint; a manual `ssh -L` forward appears in no config, so
 *        this is the only check that can see it.
 * @returns {Promise<{ok: boolean, reason: string, url: string, port: string}>}
 *          `url` is the literal-loopback origin to mint against, and is empty
 *          on every refusal so a caller cannot use it by accident.
 */
async function decideLocalMint({ backendUrl, getRemoteHost, getPortOwner } = {}) {
  const refuse = (reason, port = "") => ({ ok: false, reason, url: "", port });

  const literalUrl = literalLoopbackUrl(backendUrl);
  // URL.port is "" for a scheme-default port, and every lookup below is keyed by
  // port: an empty key would consult the config of a gateway we are not talking
  // to and probe a port we are not connected to.
  const port = defaultedPort(backendUrl);
  if (!literalUrl || !port) return refuse("not-loopback", port);

  // A caller that cannot answer "is this gateway mine?" gets no mint. This is
  // the fail-closed default that keeps a future call site from re-opening the
  // disclosure by omitting an argument.
  if (typeof getRemoteHost !== "function" || typeof getPortOwner !== "function") {
    return refuse("no-locality-evidence", port);
  }

  let remoteHost = "";
  try {
    remoteHost = String((await getRemoteHost(port)) || "");
  } catch {
    // Config we cannot read cannot clear the target. Refuse rather than assume.
    return refuse("remote-config-unreadable", port);
  }
  if (remoteHost) return refuse("remote-configured", port);

  let owner = "unknown";
  try {
    owner = String((await getPortOwner(port)) || "unknown");
  } catch {
    owner = "unknown";
  }
  if (!OWN_GATEWAY_OWNERS.has(owner)) return refuse(`listener-${owner}`, port);

  return { ok: true, reason: `listener-${owner}`, url: literalUrl, port };
}

async function requestLocalToken(http, backendUrl, secret) {
  const literalUrl = literalLoopbackUrl(backendUrl);
  if (!literalUrl) return "";
  return new Promise((resolve) => {
    const req = http.get(
      `${literalUrl}/api/token/local`,
      { headers: { "X-Local-Secret": secret }, timeout: 5000 },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          resolve("");
          return;
        }
        let data = "";
        res.on("error", () => resolve(""));
        res.on("data", (chunk) => { data += chunk; });
        res.on("end", () => {
          try { resolve(JSON.parse(data).token || ""); } catch { resolve(""); }
        });
      },
    );
    req.on("error", () => resolve(""));
    req.on("timeout", () => { req.destroy(); resolve(""); });
  });
}

async function fetchLocalToken({
  backendUrl,
  resolveHome,
  path,
  fs,
  http,
  getRemoteHost,
  getPortOwner,
  log = () => {},
}) {
  // Locality BEFORE the read: reading the secret is the first step of disclosing
  // it, and a refusal that still read it would leave the value in this process
  // for every later mistake to find.
  const verdict = await decideLocalMint({ backendUrl, getRemoteHost, getPortOwner });
  if (!verdict.ok) {
    // Never silent. A skipped mint and a rejected secret both surface to the
    // user as the token prompt, so the log line is the only place the two can be
    // told apart.
    log(`local mint skipped for ${backendUrl}: ${verdict.reason}`);
    return "";
  }
  let secret = "";
  try {
    const authoritativeHome = resolveHome();
    secret = fs.readFileSync(path.join(authoritativeHome, ".local_secret"), "utf8").trim();
  } catch {
    // A missing/unreadable authoritative secret is an ordinary token miss.
  }
  if (!secret) return "";
  return requestLocalToken(http, verdict.url, secret);
}

module.exports = { fetchLocalToken, literalLoopbackUrl, decideLocalMint };
