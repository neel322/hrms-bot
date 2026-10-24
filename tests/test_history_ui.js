const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

async function runTests() {
  console.log("Starting History UI Tests in JSDOM...");

  const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8");
  const js = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");

  const dom = new JSDOM(html, {
    url: "http://localhost:8000/",
    runScripts: "outside-only",
  });

  const { window } = dom;

  const markedJs = fs.readFileSync(path.join(__dirname, "../static/vendor/marked.min.js"), "utf8");
  const purifyJs = fs.readFileSync(path.join(__dirname, "../static/vendor/purify.min.js"), "utf8");
  window.eval(markedJs);
  window.eval(purifyJs);

  // Mock essentials
  window.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ([]),
  });
  window.sessionStorage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  };

  window.eval(js);

  const mHistoryCount = window.document.getElementById("mHistoryCount");
  const mHistorySubtext = window.document.getElementById("mHistorySubtext");
  const mHistoryMetricItem = window.document.getElementById("mHistoryMetricItem");

  assert(mHistoryCount, "mHistoryCount element must exist");
  assert(mHistorySubtext, "mHistorySubtext element must exist");

  // Check 1: Tooltip and Label
  console.log("Check 1: Metric label and tooltip");
  const mLabel = mHistoryMetricItem.querySelector(".m-label");
  assert.strictEqual(mLabel.textContent.trim(), "History sent", "Metric label must be 'History sent'");
  const expectedTooltip = "Earlier messages (your questions and the assistant's replies) sent with this query. The query itself is not counted.";
  assert.strictEqual(mHistoryMetricItem.title, expectedTooltip, "Metric item tooltip must match");

  // Check 2: New chat resets metric to 0 and clears subtitle
  console.log("Check 2: New chat resets to 0 and clears subtitle");
  window.setHistoryCount(10, { userCount: 5, assistantCount: 5 });
  assert.strictEqual(mHistoryCount.textContent, "10");
  assert.strictEqual(mHistorySubtext.textContent, "5 questions + 5 replies");

  window.startNewChat();
  assert.strictEqual(mHistoryCount.textContent, "0", "#mHistoryCount must be 0 after startNewChat()");
  assert.strictEqual(mHistorySubtext.textContent, "", "mHistorySubtext must be empty string after startNewChat()");

  // Check 3: Logout/Login resets to 0 and clears subtitle
  console.log("Check 3: resetForNewIdentity resets to 0 and clears subtitle");
  window.setHistoryCount(6, { userCount: 3, assistantCount: 3 });
  window.resetForNewIdentity(null);
  assert.strictEqual(mHistoryCount.textContent, "0", "#mHistoryCount must be 0 after resetForNewIdentity()");
  assert.strictEqual(mHistorySubtext.textContent, "", "mHistorySubtext must be empty string after resetForNewIdentity()");

  // Check 4: Loading past session with 10 messages sets 10 and subtitle
  console.log("Check 4: Loading past session with 10 messages");
  const tenMessages = Array.from({ length: 10 }, (_, i) => ({
    id: i + 1,
    role: i % 2 === 0 ? "user" : "assistant",
    content: `Message ${i + 1}`,
  }));
  window.fetch = async (url) => {
    if (String(url).includes("/history")) {
      return { ok: true, status: 200, json: async () => tenMessages };
    }
    return { ok: true, status: 200, json: async () => [] };
  };
  await window.loadSession("session_10");
  assert.strictEqual(mHistoryCount.textContent, "10", "#mHistoryCount must show 10");
  assert.strictEqual(mHistorySubtext.textContent, "5 questions + 5 replies", "Subtitle must show '5 questions + 5 replies'");

  // Check 5: context_info with history_omitted > 0
  console.log("Check 5: context_info with omitted history renders warning subtitle");
  window.setHistoryCount(10, {
    history_count: 10,
    history_total: 14,
    history_omitted: 4,
    history_user: 5,
    history_assistant: 5,
  });
  assert.strictEqual(mHistoryCount.textContent, "10");
  assert(mHistorySubtext.classList.contains("m-subtext-omitted"), "Must have class m-subtext-omitted");
  assert.strictEqual(
    mHistorySubtext.textContent.trim(),
    "10 of 14 sent (4 oldest not sent)",
    "Text content must show '10 of 14 sent (4 oldest not sent)'"
  );
  assert(mHistorySubtext.innerHTML.includes("text-warning"), "Must style omitted note with text-warning class");

  // Check 6: context_info with 0 count on fresh chat
  console.log("Check 6: context_info with 0 count renders '0 messages'");
  window.setHistoryCount(0, {
    history_count: 0,
    history_total: 0,
    history_omitted: 0,
    history_user: 0,
    history_assistant: 0,
  });
  assert.strictEqual(mHistoryCount.textContent, "0");
  assert.strictEqual(mHistorySubtext.textContent, "0 messages");

  console.log("✅ All History UI JSDOM tests passed!");
}

runTests().catch((err) => {
  console.error("❌ Test failed:", err);
  process.exit(1);
});
