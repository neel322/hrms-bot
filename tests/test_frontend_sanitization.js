/**
 * Frontend Sanitization Automated Test using Node + jsdom
 * Verifies FAIL-CLOSED SANITIZING:
 * When DOMPurify is unavailable, the frontend MUST fall back to escapeHtml(text).
 * Never insert unsanitized HTML into the DOM.
 */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

async function runSanitizationTests() {
  console.log("Starting Fail-Closed Sanitization JSDOM Tests...");

  const htmlContent = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf-8");
  const jsContent = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf-8");
  const markedJs = fs.readFileSync(path.join(__dirname, "../static/vendor/marked.min.js"), "utf-8");

  // 1. Setup DOM WITHOUT DOMPurify (simulate missing or blocked DOMPurify)
  const dom = new JSDOM(htmlContent, {
    runScripts: "outside-only",
    url: "http://localhost:8000/",
  });
  const { window } = dom;

  // Load marked, but explicitly DO NOT load DOMPurify!
  window.eval(markedJs);
  delete window.DOMPurify;
  assert.strictEqual(typeof window.DOMPurify, "undefined", "DOMPurify must be undefined for this test");

  // Mock global fetch & dependencies
  window.fetch = async (url) => {
    const urlStr = String(url);
    if (urlStr.includes("/auth/config")) return { ok: true, json: async () => ({ demo_mode: true }) };
    if (urlStr.includes("/auth/session")) return { ok: true, json: async () => ({ authenticated: false }) };
    return { ok: true, json: async () => ({}) };
  };

  // Evaluate app.js
  window.eval(jsContent);
  await new Promise(r => setTimeout(r, 60));

  const xssPayload = '<img src=x onerror=alert(1)>';
  const scriptPayload = '<script>alert("pwned")</script>';
  const svgPayload = '<svg onload=alert(document.cookie)>';

  console.log("Check 1: renderMarkdownSafely fails closed without DOMPurify");
  assert.strictEqual(typeof window.renderMarkdownSafely, "function", "renderMarkdownSafely function must exist");

  const safeResult = window.renderMarkdownSafely(xssPayload);
  assert(!safeResult.includes("<img"), "Must NOT contain raw <img> tag");
  assert(safeResult.includes("&lt;img"), "Must be HTML-escaped");

  const testDiv = window.document.createElement("div");
  testDiv.innerHTML = safeResult;
  assert.strictEqual(testDiv.querySelector("img"), null, "No <img> element should ever be created in DOM");
  assert.strictEqual(testDiv.querySelectorAll("*").length, 0, "No HTML child tags should exist");

  console.log("Check 2: Script and SVG payloads are safely escaped");
  const safeScript = window.renderMarkdownSafely(scriptPayload);
  testDiv.innerHTML = safeScript;
  assert.strictEqual(testDiv.querySelector("script"), null, "No <script> element created");

  const safeSvg = window.renderMarkdownSafely(svgPayload);
  testDiv.innerHTML = safeSvg;
  assert.strictEqual(testDiv.querySelector("svg"), null, "No <svg> element created");

  console.log("Check 3: loadSessionHistory with DOMPurify unavailable");
  window.fetch = async (url) => {
    const urlStr = String(url);
    if (urlStr.includes("/history")) {
      return {
        ok: true,
        json: async () => [
          { id: 1, role: "user", content: "Hello" },
          { id: 2, role: "assistant", content: xssPayload }
        ]
      };
    }
    return { ok: true, json: async () => [] };
  };

  await window.loadSession("test-session-123", null, false);
  const chatStream = window.document.getElementById("chatStream");
  assert.strictEqual(chatStream.querySelector("img"), null, "Restored history must not create <img> element in DOM");
  assert(chatStream.textContent.includes("<img src=x onerror=alert(1)>"), "Payload must be rendered as literal text");

  console.log("Check 4: User message rendering with XSS payload");
  window.appendUserMessage(xssPayload);
  assert.strictEqual(chatStream.querySelector("img"), null, "User message must not create <img> element in DOM");

  console.log("Check 5: Behavior when DOMPurify IS available");
  const purifyJs = fs.readFileSync(path.join(__dirname, "../static/vendor/purify.min.js"), "utf-8");
  window.eval(purifyJs);
  assert(window.DOMPurify && typeof window.DOMPurify.sanitize === "function", "DOMPurify must now be active");

  const purified = window.renderMarkdownSafely("**Safe bold text** " + xssPayload);
  testDiv.innerHTML = purified;
  // DOMPurify removes onerror attribute from img or strips it
  const imgTag = testDiv.querySelector("img");
  if (imgTag) {
    assert(!imgTag.hasAttribute("onerror"), "DOMPurify must strip onerror attribute");
  }
  assert(testDiv.querySelector("strong") || testDiv.innerHTML.includes("<strong>"), "Markdown bold formatting parsed when DOMPurify present");

  console.log("✅ All Fail-Closed Sanitization JSDOM Tests Passed Successfully!");
}

runSanitizationTests().catch(err => {
  console.error("❌ Sanitization test failed:", err);
  process.exit(1);
});
