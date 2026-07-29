// Component 27 (DESIGN.md §3e) — UI admin-token wiring. Same pattern as
// ui/citation.test.js and ui/ingest.test.js: regex-extract the
// <script id="auth-logic"> block from index.html and run it in a bare Node vm
// context. The block is deliberately pure (no document/window/fetch/
// localStorage) so it is testable here; the click handlers and the actual
// storage read live in the page's main script.
//
// Why this component exists: until now the UI sent an Authorization header on
// exactly ONE call (the metrics poll). Every mutation — register, presign,
// retry, delete, document upload — sent none. With ADMIN_TOKEN set those all
// 401, so the app only functioned with auth DISABLED. That is not a viable
// state to deploy in either direction, which is why this had to land before
// the Fly deploy alongside the auth layer itself (component 25).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

function loadAuthLogic() {
  const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  const m = html.match(/<script id="auth-logic">([\s\S]*?)<\/script>/);
  assert.ok(m, 'index.html must contain a <script id="auth-logic"> block');
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox);
  return sandbox;
}

const A = loadAuthLogic();

// The vm sandbox is a separate realm, so objects it returns have a different
// Object.prototype and deepStrictEqual rejects them even when the contents are
// identical. Copy into this realm before comparing.
const plain = (o) => Object.assign({}, o);

test("authHeaders: adds a Bearer header when a token is present", () => {
  assert.deepEqual(plain(A.authHeaders("s3cret")), { Authorization: "Bearer s3cret" });
});

test("authHeaders: sends NOTHING when no token is set", () => {
  // Dev stacks run with ADMIN_TOKEN empty; sending `Bearer ` (empty) would be
  // a malformed header rather than an absent one.
  assert.deepEqual(plain(A.authHeaders("")), {});
  assert.deepEqual(plain(A.authHeaders(null)), {});
  assert.deepEqual(plain(A.authHeaders(undefined)), {});
});

test("authHeaders: trims surrounding whitespace from a pasted token", () => {
  // Copy-pasting a secret out of a terminal or password manager very commonly
  // drags in a trailing newline or space, which would 401 with no clue why.
  assert.deepEqual(plain(A.authHeaders("  s3cret\n")), { Authorization: "Bearer s3cret" });
});

test("authHeaders: a whitespace-only token counts as no token", () => {
  assert.deepEqual(plain(A.authHeaders("   ")), {});
});

test("withAuth: merges the bearer header into existing request headers", () => {
  const out = plain(A.withAuth({ "Content-Type": "application/json" }, "s3cret"));
  assert.deepEqual(out, {
    "Content-Type": "application/json",
    Authorization: "Bearer s3cret",
  });
});

test("withAuth: leaves headers untouched when there is no token", () => {
  const out = plain(A.withAuth({ "Content-Type": "application/json" }, ""));
  assert.deepEqual(out, { "Content-Type": "application/json" });
});

test("withAuth: tolerates being called with no headers at all", () => {
  assert.deepEqual(plain(A.withAuth(undefined, "s3cret")), { Authorization: "Bearer s3cret" });
  assert.deepEqual(plain(A.withAuth(undefined, "")), {});
});

test("authErrorMessage: 401 explains the fix instead of echoing the status", () => {
  const msg = A.authErrorMessage(401, "");
  assert.match(msg, /admin token/i);
});

test("authErrorMessage: 503 names the server-side misconfiguration", () => {
  // Component 25 returns 503 when the SERVER is missing ADMIN_TOKEN — the
  // user pasting a token cannot fix that, so it must not be reported as a
  // bad-token error.
  const msg = A.authErrorMessage(503, "Server is missing ADMIN_TOKEN");
  assert.match(msg, /server/i);
  assert.doesNotMatch(msg, /paste|enter your/i);
});

test("authErrorMessage: 429 tells the user to slow down, not to re-auth", () => {
  const msg = A.authErrorMessage(429, "");
  assert.match(msg, /too many|slow down|rate/i);
  assert.doesNotMatch(msg, /admin token/i);
});

test("authErrorMessage: other errors fall back to the server's own detail", () => {
  assert.equal(A.authErrorMessage(400, "kind must be one of ..."),
               "kind must be one of ...");
});

test("authErrorMessage: never returns an empty string", () => {
  assert.ok(A.authErrorMessage(500, "").length > 0);
});

// ── Admin-token box visibility ──────────────────────────────────────────────
// The admin token is a CROSS-TENANT operator credential (it can name any
// tenant via X-User-Id) living in localStorage. Once Auth0 provides real,
// cryptographically-scoped logins, keeping it pasteable into the self-serve UI
// undercuts the boundary Auth0 establishes — a signed-in user is scoped to
// their own data, an admin-token holder is scoped to nothing.
//
// It is hidden rather than deleted because AUTH0_* unset is a supported mode
// (.env.example ships it empty). With no identity provider AND an ADMIN_TOKEN
// set, that box is the only way the UI can mutate anything — removing it
// outright would re-break exactly what component 27 fixed.

test("adminTokenBoxVisible: hidden once Auth0 is providing real logins", () => {
  assert.equal(A.adminTokenBoxVisible(true), false);
});

test("adminTokenBoxVisible: shown when there is no identity provider", () => {
  assert.equal(A.adminTokenBoxVisible(false), true);
});

test("adminTokenBoxVisible: treats missing/undefined config as no-Auth0", () => {
  // A failed /api/config fetch must not strand the operator with no way in.
  assert.equal(A.adminTokenBoxVisible(undefined), true);
  assert.equal(A.adminTokenBoxVisible(null), true);
});
