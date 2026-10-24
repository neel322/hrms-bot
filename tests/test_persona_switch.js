const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

async function testLogoutAndLoginSwitch() {
  const htmlContent = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf-8");
  const jsContent = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf-8");
  const markedJs = fs.readFileSync(path.join(__dirname, "../static/vendor/marked.min.js"), "utf-8");
  const purifyJs = fs.readFileSync(path.join(__dirname, "../static/vendor/purify.min.js"), "utf-8");

  const dom = new JSDOM(htmlContent, {
    runScripts: "outside-only",
    url: "http://localhost:8000/",
  });
  const { window } = dom;

  window.eval(markedJs);
  window.eval(purifyJs);

  const users = {
    "priya.sharma@acmetech.com": {
      id: 8,
      emp_code: "ACM-008",
      name: "Priya Sharma",
      email: "priya.sharma@acmetech.com",
      role: "employee",
      department: "Engineering",
      status: "active"
    },
    "rahul.mehta@acmetech.com": {
      id: 5,
      emp_code: "ACM-005",
      name: "Rahul Mehta",
      email: "rahul.mehta@acmetech.com",
      role: "manager",
      department: "Engineering",
      status: "active"
    }
  };

  let authenticatedUser = null;

  window.fetch = async (url, options = {}) => {
    const urlStr = String(url);

    if (urlStr.includes("/auth/session")) {
      return {
        ok: true,
        json: async () => ({
          authenticated: !!authenticatedUser,
          caller: authenticatedUser
        })
      };
    }

    if (urlStr.includes("/auth/login")) {
      const body = JSON.parse(options.body || "{}");
      const user = users[body.email];
      if (user && body.password === "AcmeSecret2026!") {
        authenticatedUser = user;
        return {
          ok: true,
          json: async () => ({ message: "Login successful", employee: user })
        };
      }
      return {
        ok: false,
        status: 401,
        json: async () => ({ detail: "Invalid email or password" })
      };
    }

    if (urlStr.includes("/auth/logout")) {
      authenticatedUser = null;
      return { ok: true, json: async () => ({ message: "Logged out" }) };
    }

    if (urlStr.includes("/employees")) {
      return { ok: true, json: async () => Object.values(users) };
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
    if (urlStr.includes("/audit")) {
      return { ok: true, json: async () => ({ logs: [] }) };
    }

    return { ok: true, json: async () => ({}) };
  };

  // Run app.js
  window.eval(jsContent);
  await new Promise((r) => setTimeout(r, 100));

  const chatStream = window.document.getElementById("chatStream");
  const loginForm = window.document.getElementById("loginForm");
  const loginEmail = window.document.getElementById("loginEmail");
  const loginPassword = window.document.getElementById("loginPassword");
  const btnLogout = window.document.getElementById("btnLogout");
  const badgeName = window.document.getElementById("badgeName");
  const badgeRole = window.document.getElementById("badgeRole");

  // Verify persona dropdown was removed from DOM
  assert.strictEqual(window.document.getElementById("personaSelect"), null, "personaSelect should not exist in DOM");
  assert.strictEqual(window.document.getElementById("personaSelectGroup"), null, "personaSelectGroup should not exist in DOM");

  // Step 1: Log in as Priya Sharma
  console.log("Step 1: Logging in as Priya Sharma (employee)...");
  loginEmail.value = "priya.sharma@acmetech.com";
  loginPassword.value = "AcmeSecret2026!";
  loginForm.dispatchEvent(new window.Event("submit"));
  await new Promise((r) => setTimeout(r, 100));

  assert.strictEqual(window.STATE.activeUser.emp_code, "ACM-008", "Active user should be Priya Sharma");
  assert.strictEqual(badgeName.textContent, "Priya Sharma");
  assert.strictEqual(badgeRole.textContent, "employee");
  const priyaSessionId = window.STATE.sessionId;
  assert(priyaSessionId && priyaSessionId.length > 8, "Expected valid session ID for Priya");

  // Simulate Priya having chat messages
  const userMsg = window.document.createElement("div");
  userMsg.className = "chat-message user-msg";
  userMsg.textContent = "What is my casual leave balance?";
  chatStream.appendChild(userMsg);

  const asstMsg = window.document.createElement("div");
  asstMsg.className = "chat-message assistant-msg";
  asstMsg.textContent = "You have 11 casual leaves remaining.";
  chatStream.appendChild(asstMsg);

  assert(chatStream.textContent.includes("What is my casual leave balance?"), "Chat contains Priya's query");

  // Step 2: Logout
  console.log("Step 2: Logging out...");
  btnLogout.dispatchEvent(new window.Event("click"));
  await new Promise((r) => setTimeout(r, 100));

  assert.strictEqual(chatStream.innerHTML, "", "Chat container must be empty immediately on logout");
  assert.strictEqual(window.STATE.activeUser, null, "Active user must be null after logout");

  // Step 3: Log in as Rahul Mehta (manager)
  console.log("Step 3: Logging in as Rahul Mehta (manager)...");
  loginEmail.value = "rahul.mehta@acmetech.com";
  loginPassword.value = "AcmeSecret2026!";
  loginForm.dispatchEvent(new window.Event("submit"));
  await new Promise((r) => setTimeout(r, 100));

  assert.strictEqual(window.STATE.activeUser.emp_code, "ACM-005", "Active user should be Rahul Mehta");
  assert.strictEqual(badgeName.textContent, "Rahul Mehta");
  assert.strictEqual(badgeRole.textContent, "manager");

  // Step 4: Verify chat container does NOT contain Priya's messages
  const rahulChatText = chatStream.textContent;
  assert(!rahulChatText.includes("What is my casual leave balance?"), "Priya's messages must NOT be in Rahul's chat!");
  assert(!rahulChatText.includes("You have 11 casual leaves remaining"), "Priya's assistant response must NOT be in Rahul's chat!");

  // Step 5: Verify session ID changed
  const rahulSessionId = window.STATE.sessionId;
  assert.notStrictEqual(rahulSessionId, priyaSessionId, "Session ID must change when logging in as a different user");
  assert(rahulSessionId && rahulSessionId.length > 8, "Expected valid session ID for Rahul");

  // Step 6: Verify inspector is reset
  assert.strictEqual(window.STATE.isStreaming, false, "STATE.isStreaming must be false");
  assert.strictEqual(window.STATE.turnStats.tools.length, 0, "Turn tools must be reset to 0");

  console.log("✅ testPersonaSwitch (logout -> login switch) passed successfully!");
}

testLogoutAndLoginSwitch().catch((err) => {
  console.error("Test failed:", err);
  process.exit(1);
});
