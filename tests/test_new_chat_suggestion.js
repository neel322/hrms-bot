const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

async function runTests() {
  console.log("Starting New Chat Suggestion JSDOM Tests...");

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

  // In-memory sessionStorage mock for JSDOM
  const storageData = {};
  window.sessionStorage = {
    getItem: (k) => (k in storageData ? storageData[k] : null),
    setItem: (k, v) => { storageData[k] = String(v); },
    removeItem: (k) => { delete storageData[k]; },
    clear: () => { Object.keys(storageData).forEach(k => delete storageData[k]); },
    key: (i) => Object.keys(storageData)[i] || null,
    get length() { return Object.keys(storageData).length; },
  };

  // Mock fetch
  window.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ([]),
  });

  window.eval(js);

  const newChatSuggestion = window.document.getElementById("newChatSuggestion");
  const ncsText = window.document.getElementById("ncsText");
  const btnNcsStart = window.document.getElementById("btnNcsStart");
  const btnNcsKeep = window.document.getElementById("btnNcsKeep");
  const btnNcsClose = window.document.getElementById("btnNcsClose");
  const chatInput = window.document.getElementById("chatInput");
  const btnSend = window.document.getElementById("btnSend");

  assert(newChatSuggestion, "newChatSuggestion banner element must exist in DOM");
  assert(ncsText, "ncsText element must exist in DOM");
  assert(btnNcsStart, "btnNcsStart button must exist");
  assert(btnNcsKeep, "btnNcsKeep button must exist");
  assert(btnNcsClose, "btnNcsClose button must exist");

  // Check Accessibility Attributes
  assert.strictEqual(newChatSuggestion.getAttribute("role"), "status", "Must have role='status'");
  assert.strictEqual(newChatSuggestion.getAttribute("aria-live"), "polite", "Must have aria-live='polite'");

  // Helper to simulate turn completion
  function simulateCompletedTurn() {
    window.STATE.completedQueryCount = (window.STATE.completedQueryCount || 0) + 1;
    window.checkAndShowNewChatSuggestion();
  }

  // --- Check 1: Initial state & queries 1 to 3 ---
  console.log("Check 1: No box for 1 to 3 completed queries");
  window.startNewChat();
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden on fresh session");
  assert.strictEqual(window.getCompletedQueryCount(), 0, "Query count must be 0 initially");

  simulateCompletedTurn(); // query 1
  assert.strictEqual(window.getCompletedQueryCount(), 1);
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden after query 1");

  simulateCompletedTurn(); // query 2
  assert.strictEqual(window.getCompletedQueryCount(), 2);
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden after query 2");

  simulateCompletedTurn(); // query 3
  assert.strictEqual(window.getCompletedQueryCount(), 3);
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden after query 3 (SUGGEST_NEW_CHAT_AFTER = 3)");

  // --- Check 2: Query 4 triggers suggestion box ---
  console.log("Check 2: Box appears after 4th completed reply");
  simulateCompletedTurn(); // query 4
  assert.strictEqual(window.getCompletedQueryCount(), 4);
  assert(!newChatSuggestion.classList.contains("hidden"), "Box must be visible after query 4");
  assert(ncsText.textContent.includes("4 questions"), "Text must say 'This chat has 4 questions...'");
  assert(
    ncsText.textContent.includes("For a new topic, a new chat gives faster, more focused answers."),
    "Must include friendly advice text"
  );

  // Verify input stays usable while visible
  assert.strictEqual(chatInput.disabled, false, "Chat input must stay usable while suggestion is visible");
  assert.strictEqual(btnSend.disabled, false, "Send button must stay usable while suggestion is visible");

  // --- Check 3: Box never appears while reply is streaming ---
  console.log("Check 3: Box is hidden while streaming");
  window.STATE.isStreaming = true;
  window.checkAndShowNewChatSuggestion();
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden while isStreaming is true");
  window.STATE.isStreaming = false;
  window.checkAndShowNewChatSuggestion();
  assert(!newChatSuggestion.classList.contains("hidden"), "Box becomes visible again after streaming");

  // --- Check 4: Dismiss with 'Keep chatting' and repeat interval ---
  console.log("Check 4: 'Keep chatting' dismisses and stays hidden until 5 more queries");
  const sid = window.STATE.sessionId;
  btnNcsKeep.click();
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden after clicking 'Keep chatting'");

  // Verify dismiss state stored per session id and contains ONLY number
  const storedVal = window.sessionStorage.getItem("hrms_dismiss_suggest_" + sid);
  assert.strictEqual(storedVal, "4", "Stored dismiss value must be '4'");
  assert(!storedVal.includes("question"), "Stored dismiss value must contain only the count number, no message text");

  // Queries 5, 6, 7, 8 must NOT show the box
  for (let q = 5; q <= 8; q++) {
    simulateCompletedTurn();
    assert.strictEqual(window.getCompletedQueryCount(), q);
    assert(newChatSuggestion.classList.contains("hidden"), `Box must stay hidden after query ${q}`);
  }

  // Query 9 (4 + 5 = 9): Box reappears!
  console.log("Check 5: Box reappears after 5 more queries (9th query)");
  simulateCompletedTurn();
  assert.strictEqual(window.getCompletedQueryCount(), 9);
  assert(!newChatSuggestion.classList.contains("hidden"), "Box must reappear on 9th query");
  assert(ncsText.textContent.includes("9 questions"), "Text must say 'This chat has 9 questions...'");

  // Dismiss with Close (x) button
  btnNcsClose.click();
  assert(newChatSuggestion.classList.contains("hidden"), "Close button must dismiss the box");
  assert.strictEqual(window.sessionStorage.getItem("hrms_dismiss_suggest_" + sid), "9");

  // Esc key dismiss
  newChatSuggestion.classList.remove("hidden");
  assert(!newChatSuggestion.classList.contains("hidden"));
  const escEvent = new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true });
  newChatSuggestion.dispatchEvent(escEvent);
  assert(newChatSuggestion.classList.contains("hidden"), "Esc key inside box must dismiss it");

  // --- Check 6: 'Start new chat' button from box ---
  console.log("Check 6: 'Start new chat' clears chat, resets session id, and hides box");
  const oldSid = window.STATE.sessionId;
  window.setHistoryCount(12);
  assert.strictEqual(window.STATE.historyCount, 12);

  btnNcsStart.click();
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden after start new chat");
  assert.notStrictEqual(window.STATE.sessionId, oldSid, "Session ID must change");
  assert.strictEqual(window.STATE.historyCount, 0, "History count must reset to 0");
  assert.strictEqual(window.getCompletedQueryCount(), 0, "Completed query count must reset to 0");

  // --- Check 7: Logout / Login switches ---
  console.log("Check 7: Logout then login as another user shows no box");
  window.resetForNewIdentity(null);
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden after logout");
  assert.strictEqual(window.getCompletedQueryCount(), 0);

  const rahulUser = { id: 5, emp_code: "ACM-005", name: "Rahul Mehta", role: "manager" };
  window.resetForNewIdentity(rahulUser);
  assert(newChatSuggestion.classList.contains("hidden"), "Box must be hidden on fresh user login");

  // --- Check 8: Restoring past session with 6 queries shows box ---
  console.log("Check 8: Restoring past session with 6 queries shows box");
  const sixQueriesHistory = [];
  for (let i = 1; i <= 6; i++) {
    sixQueriesHistory.push({ id: 2 * i - 1, role: "user", content: `Question ${i}`, status: "ok" });
    sixQueriesHistory.push({ id: 2 * i, role: "assistant", content: `Answer ${i}`, status: "ok" });
  }

  window.fetch = async (url) => {
    if (String(url).includes("/history")) {
      return { ok: true, status: 200, json: async () => sixQueriesHistory };
    }
    return { ok: true, status: 200, json: async () => [] };
  };

  await window.loadSession("session_with_6_queries");
  assert.strictEqual(window.getCompletedQueryCount(), 6, "Must count 6 completed replies from history");
  assert(!newChatSuggestion.classList.contains("hidden"), "Box must be visible for restored 6-query session");
  assert(ncsText.textContent.includes("6 questions"), "Text must reflect '6 questions'");

  // --- Check 9: Restoring past session with 2 queries does NOT show box ---
  console.log("Check 9: Restoring past session with 2 queries does NOT show box");
  const twoQueriesHistory = [
    { id: 1, role: "user", content: "Q1", status: "ok" },
    { id: 2, role: "assistant", content: "A1", status: "ok" },
    { id: 3, role: "user", content: "Q2", status: "ok" },
    { id: 4, role: "assistant", content: "A2", status: "ok" },
  ];
  window.fetch = async (url) => {
    if (String(url).includes("/history")) {
      return { ok: true, status: 200, json: async () => twoQueriesHistory };
    }
    return { ok: true, status: 200, json: async () => [] };
  };
  await window.loadSession("session_with_2_queries");
  assert.strictEqual(window.getCompletedQueryCount(), 2);
  assert(newChatSuggestion.classList.contains("hidden"), "Box must NOT show for session with 2 queries");

  console.log("✅ All New Chat Suggestion JSDOM tests passed successfully!");
}

runTests().catch((err) => {
  console.error("❌ Test failed:", err);
  process.exit(1);
});
