// Component 8 (DESIGN.md) — UI citation render. Tests the ACTUAL shipped
// logic, not a duplicated copy: extracts the <script id="citation-logic">
// block directly out of index.html (regex) and runs it in a bare Node `vm`
// context. That block is pure/DOM-free by design (no document/window/fetch),
// so no DOM stubbing is needed — if it ever touches the DOM, this breaks
// loudly rather than silently testing stale logic.
//
// No new file/route was added for this: the app serves index.html as a
// single inline-script page with no static-asset mount, so a separate
// citation.js would 404 in production. Keeping the pure helpers in their own
// <script> tag (same page, same global scope) avoids that entirely.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

function loadCitationLogic() {
  const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  const m = html.match(/<script id="citation-logic">([\s\S]*?)<\/script>/);
  assert.ok(m, "index.html must contain a <script id=\"citation-logic\"> block");
  const sandbox = {};
  vm.createContext(sandbox);
  vm.runInContext(m[1], sandbox);
  return sandbox;
}

const C = loadCitationLogic();

test("citeKind reads c.kind, defaults to video", () => {
  assert.equal(C.citeKind({ kind: "paper" }), "paper");
  assert.equal(C.citeKind({ kind: "deck" }), "deck");
  assert.equal(C.citeKind({ kind: "video" }), "video");
  assert.equal(C.citeKind({}), "video");
  assert.equal(C.citeKind(null), "video");
});

test("citeIsDocument true for paper/deck and any other non-video kind", () => {
  assert.equal(C.citeIsDocument({ kind: "paper" }), true);
  assert.equal(C.citeIsDocument({ kind: "deck" }), true);
  assert.equal(C.citeIsDocument({ kind: "video" }), false);
  assert.equal(C.citeIsDocument({}), false);
  // retrieve()'s defensive fallback (src/rag/search.py) for a chunk payload
  // missing `kind` entirely -- must route to the document path, not video's.
  assert.equal(C.citeIsDocument({ kind: "document" }), true);
});

test("citeLabel: video uses timestamp, paper/deck use the locator", () => {
  assert.equal(C.citeLabel({ kind: "video", timestamp: "14:22" }), "14:22");
  assert.equal(C.citeLabel({ kind: "paper", locator: { page: 4 } }), "Page 4");
  assert.equal(C.citeLabel({ kind: "deck", locator: { slide: 12 } }), "Slide 12");
  assert.equal(C.citeLabel({}), "");
});

test("citeOpenUrl: arXiv paper uris (no literal .pdf suffix) still get a #page anchor", () => {
  // arXiv serves PDFs at e.g. https://arxiv.org/pdf/1706.03762 -- no ".pdf" in
  // the URL string at all (Content-Type is set by the server, not the path).
  // This is our corpus's primary paper format (benchmark/corpus.json) -- a
  // URL-suffix heuristic would silently drop the anchor for exactly this case.
  assert.equal(
    C.citeOpenUrl({ kind: "paper", uri: "https://arxiv.org/pdf/1706.03762", locator: { page: 4 } }),
    "https://arxiv.org/pdf/1706.03762#page=4"
  );
});

test("citeOpenUrl: deck uris (.pdf or otherwise) get a #page anchor from the slide number", () => {
  assert.equal(
    C.citeOpenUrl({ kind: "deck", uri: "https://icml.cc/media/slides.pdf", locator: { slide: 12 } }),
    "https://icml.cc/media/slides.pdf#page=12"
  );
  // A .pptx target can't render inline anyway (the browser just downloads
  // it), so an ignored fragment is harmless -- no suffix-sniffing needed.
  assert.equal(
    C.citeOpenUrl({ kind: "deck", uri: "storage://decks/kdd-keynote.pptx", locator: { slide: 3 } }),
    "storage://decks/kdd-keynote.pptx#page=3"
  );
});

test("citeOpenUrl: missing uri returns null (nothing to open)", () => {
  assert.equal(C.citeOpenUrl({ kind: "paper", locator: { page: 1 } }), null);
});

test("citeOpenUrl: an existing #fragment or ?query on the pdf uri is still detected", () => {
  assert.equal(
    C.citeOpenUrl({ kind: "paper", uri: "https://x/paper.pdf?download=1", locator: { page: 2 } }),
    "https://x/paper.pdf?download=1#page=2"
  );
});
