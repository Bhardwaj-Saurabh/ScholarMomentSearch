// Component 11 (DESIGN.md) — self-serve Paper/Deck ingest tab. Tests the
// ACTUAL shipped logic the same way ui/citation.test.js does: regex-extracts
// the <script id="ingest-logic"> block from index.html and runs it in a bare
// Node vm context. Kept pure/DOM-free (no document/window/fetch) on purpose —
// the click handlers that call fetch() live in the page's main script and are
// exercised manually (browser) / via the Python contract tests in
// tests/test_admin_api.py, not here.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

function loadIngestLogic() {
  const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  const m = html.match(/<script id="ingest-logic">([\s\S]*?)<\/script>/);
  assert.ok(m, "index.html must contain a <script id=\"ingest-logic\"> block");
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox);
  return sandbox;
}

const I = loadIngestLogic();

test("docBadge: lifecycle statuses map to a distinct icon + label", () => {
  assert.equal(I.docBadge("pending").label, "waiting (fair queue)");
  assert.equal(I.docBadge("fetching").label, "fetching…");
  assert.equal(I.docBadge("parsing").label, "parsing…");
  assert.equal(I.docBadge("embedding").label, "embedding…");
  const indexed = I.docBadge("indexed");
  assert.equal(indexed.icon, "✓");
  const failed = I.docBadge("failed");
  assert.equal(failed.icon, "⚠");
  assert.equal(I.docBadge("skipped").label, "duplicate");
});

test("docBadge: unknown status falls back instead of throwing", () => {
  const b = I.docBadge("some-future-status");
  assert.equal(b.label, "some-future-status");
});

test("buildDocumentPayload: valid kind+uri builds the /admin/documents body", () => {
  // Field-by-field, not assert.deepEqual: r.body is an object literal created
  // inside the vm sandbox's separate realm, so it fails deepStrictEqual's
  // cross-realm prototype check even with identical own properties.
  const r = I.buildDocumentPayload("paper", "https://arxiv.org/pdf/1706.03762", "Attention");
  assert.equal(r.ok, true);
  assert.equal(r.body.kind, "paper");
  assert.equal(r.body.uri, "https://arxiv.org/pdf/1706.03762");
  assert.equal(r.body.title, "Attention");
});

test("buildDocumentPayload: blank title is omitted, not sent as empty string", () => {
  const r = I.buildDocumentPayload("deck", "https://example.com/deck.pdf", "  ");
  assert.equal(r.ok, true);
  assert.equal(r.body.title, null);
});

test("buildDocumentPayload: rejects missing uri before hitting the network", () => {
  const r = I.buildDocumentPayload("paper", "   ", "x");
  assert.equal(r.ok, false);
  assert.match(r.error, /uri/i);
});

test("buildDocumentPayload: rejects a kind outside paper|deck", () => {
  const r = I.buildDocumentPayload("video", "https://example.com/x.pdf", "x");
  assert.equal(r.ok, false);
  assert.match(r.error, /kind/i);
});
