/**
 * Frontend automated test using Node + jsdom
 * Verifies that:
 * 1. Persona dropdown and demo UI are removed
 * 2. On logout, chat DOM is cleared and active user is reset
 * 3. After logging in as a different user, chat container is empty of previous user messages,
 *    session ID has changed, and inspector is reset.
 */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

async function runTest() {
  console.log("Starting Frontend JSDOM Test for Logout & Login Switch...");

  const htmlContent = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf-8");
  const jsContent = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf-8");

  const dom = new JSDOM(htmlContent, {
    runScripts: "outside-only",
    url: "http://localhost:8000/",
  });

  const { window } = dom;

  const markedJs = fs.readFileSync(path.join(__dirname, "../static/vendor/marked.min.js"), "utf-8");
  const purifyJs = fs.readFileSync(path.join(__dirname, "../static/vendor/purify.min.js"), "utf-8");
  window.eval(markedJs);
  window.eval(purifyJs);

  const priyaUser = { id: 8, emp_code: "ACM-008", name: "Priya Sharma", email: "priya.sharma@acmetech.com", role: "employee", department: "Engineering" };
  const rahulUser = { id: 5, emp_code: "ACM-005", name: "Rahul Mehta", email: "rahul.mehta@acmetech.com", role: "manager", department: "Engineering" };

  let currentCaller = priyaUser;

  window.fetch = async (url, options = {}) => {
    const urlStr = String(url);
    if (urlStr.includes("/auth/session")) {
      return { ok: true, json: async () => ({ authenticated: !!currentCaller, caller: currentCaller }) };
    }
    if (urlStr.includes("/auth/login")) {
      const body = options.body ? JSON.parse(options.body) : {};
      if (body.email === "rahul.mehta@acmetech.com") {
        currentCaller = rahulUser;
        return { ok: true, json: async () => ({ message: "Success", employee: rahulUser }) };
      }
      currentCaller = priyaUser;
      return { ok: true, json: async () => ({ message: "Success", employee: priyaUser }) };
    }
    if (urlStr.includes("/auth/logout")) {
      currentCaller = null;
      return { ok: true, json: async () => ({ success: true }) };
    }
    if (urlStr.includes("/sessions")) {
      return { ok: true, json: async () => [] };
    }
    if (urlStr.includes("/history")) {
      return { ok: true, json: async () => [] };
    }
    if (urlStr.includes("/memories")) {
      return { ok: true, json: async () => [] };
    }
    if (urlStr.includes("/policies/files")) {
      return { ok: true, json: async () => ({ files: [] }) };
    }
    if (urlStr.includes("/employees")) {
      return { ok: true, json: async () => [priyaUser, rahulUser] };
    }
    if (urlStr.includes("/audit")) {
      return { ok: true, json: async () => ({ logs: [] }) };
    }
    return { ok: true, json: async () => ({}) };
  };

  // Execute app.js in jsdom
  window.eval(jsContent);
  await new Promise(r => setTimeout(r, 100));

  const chatStream = window.document.getElementById("chatStream");
  const btnLogout = window.document.getElementById("btnLogout");
  const loginForm = window.document.getElementById("loginForm");
  const loginEmail = window.document.getElementById("loginEmail");
  const loginPassword = window.document.getElementById("loginPassword");

  // 1. Initial State: Logged in as Priya
  console.log("Check 1: Initial state (Priya Sharma)");
  assert.strictEqual(window.document.getElementById("personaSelect"), null, "personaSelect must not exist");
  assert.strictEqual(window.document.getElementById("demoModeBanner"), null, "demoModeBanner must not exist");

  const initialSessionId = window.STATE.sessionId;
  assert(initialSessionId && initialSessionId.length > 8, "Expected valid initial session ID");
  assert.strictEqual(window.STATE.activeUser.emp_code, "ACM-008", "Expected active user to be Priya");

  // Add messages for Priya
  const dummyUserMsg = window.document.createElement("div");
  dummyUserMsg.className = "chat-message user-msg";
  dummyUserMsg.textContent = "What is my casual leave balance?";
  chatStream.appendChild(dummyUserMsg);

  const dummyAsstMsg = window.document.createElement("div");
  dummyAsstMsg.className = "chat-message assistant-msg";
  dummyAsstMsg.textContent = "You have 11 casual leaves remaining.";
  chatStream.appendChild(dummyAsstMsg);

  assert(chatStream.querySelectorAll(".chat-message").length >= 2, "Expected messages in Priya's chat");

  // 2. Logout
  console.log("Check 2: Logout clears chat DOM");
  btnLogout.dispatchEvent(new window.Event("click"));
  await new Promise(r => setTimeout(r, 100));

  assert.strictEqual(chatStream.innerHTML, "", "Chat container must be empty immediately upon logout");
  assert.strictEqual(window.STATE.activeUser, null, "Active user must be null upon logout");

  // 3. Login as Rahul Mehta (manager)
  console.log("Check 3: Login as Rahul Mehta (manager)");
  loginEmail.value = "rahul.mehta@acmetech.com";
  loginPassword.value = "AcmeSecret2026!";
  loginForm.dispatchEvent(new window.Event("submit"));
  await new Promise(r => setTimeout(r, 100));

  assert.strictEqual(window.STATE.activeUser.emp_code, "ACM-005", "Expected active user to be Rahul");
  assert.strictEqual(window.document.getElementById("badgeName").textContent, "Rahul Mehta");
  assert.strictEqual(window.document.getElementById("badgeRole").textContent, "manager");

  // Assertions:
  // a. Previous conversation messages from Priya must NOT be in chat container
  const rahulText = chatStream.textContent;
  assert(!rahulText.includes("What is my casual leave balance?"), "Priya's query must not be in Rahul's chat DOM");
  assert(!rahulText.includes("You have 11 casual leaves remaining"), "Priya's reply must not be in Rahul's chat DOM");

  // b. Session ID changed
  const rahulSessionId = window.STATE.sessionId;
  assert.notStrictEqual(rahulSessionId, initialSessionId, "Session ID must change after switching user");
  assert(rahulSessionId && rahulSessionId.length > 8, "Expected valid new session ID");

  // c. Inspector reset
  assert.strictEqual(window.STATE.isStreaming, false, "STATE.isStreaming must be false");
  assert.strictEqual(window.STATE.turnStats.tools.length, 0, "Turn stats tools must be reset");
  assert.strictEqual(window.document.getElementById("mHistoryCount").textContent, "0", "#mHistoryCount must show '0' after logout then login as another user");
  assert.strictEqual(window.STATE.historyCount, 0, "STATE.historyCount must be 0 after login as another user");

  // 4. Test startNewChat() sets #mHistoryCount to "0"
  console.log("Check 4: after startNewChat(), #mHistoryCount shows '0'");
  window.startNewChat();
  assert.strictEqual(window.document.getElementById("mHistoryCount").textContent, "0", "#mHistoryCount must show '0' after startNewChat()");
  assert.strictEqual(window.STATE.historyCount, 0, "STATE.historyCount must be 0 after startNewChat()");

  // 5. Test loading past session with 10 messages sets #mHistoryCount to "10"
  console.log("Check 5: after loading a past session with 10 messages, #mHistoryCount shows '10'");
  const tenMessages = Array.from({ length: 10 }, (_, i) => ({
    id: i + 1,
    role: i % 2 === 0 ? "user" : "assistant",
    content: `Message ${i + 1}`,
  }));
  const originalFetch = window.fetch;
  window.fetch = async (url, options = {}) => {
    const urlStr = String(url);
    if (urlStr.includes("/history")) {
      return { ok: true, json: async () => tenMessages };
    }
    return originalFetch(url, options);
  };
  await window.loadSession("session_with_10_messages");
  assert.strictEqual(window.document.getElementById("mHistoryCount").textContent, "10", "#mHistoryCount must show '10' after loading a past session with 10 messages");
  assert.strictEqual(window.STATE.historyCount, 10, "STATE.historyCount must be 10 after loading a past session with 10 messages");
  assert.strictEqual(window.document.getElementById("mHistorySubtext").textContent, "5 questions + 5 replies", "Subtitle must show '5 questions + 5 replies'");

  // 6. Test startNewChat() again resets #mHistoryCount back to "0" and clears subtitle
  window.startNewChat();
  assert.strictEqual(window.document.getElementById("mHistoryCount").textContent, "0", "#mHistoryCount must show '0' after startNewChat()");
  assert.strictEqual(window.STATE.historyCount, 0, "STATE.historyCount must be 0 after startNewChat()");
  assert.strictEqual(window.document.getElementById("mHistorySubtext").textContent, "", "Subtitle must be cleared on startNewChat()");

  console.log("✅ All Frontend JSDOM tests passed successfully!");
}

runTest().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
