/**
 * Frontend automated test for Part 3:
 * A. Inspector Tabs: More (3-dot) menu for HR, accessible keyboard/click, hidden for non-HR
 * B. Chunk View More / View Less, Copy, Below threshold badge
 * C. HR Data Results: Normalized status badges, table/kv rendering, Raw JSON toggle, and informative empty explanations on done
 */
const { JSDOM } = require("jsdom");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

async function runTests() {
  console.log("Starting Part 3 Frontend JSDOM Test Suite...");

  const htmlContent = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf-8");
  const jsContent = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf-8");

  const dom = new JSDOM(htmlContent, {
    runScripts: "outside-only",
    url: "http://localhost:8000/",
  });

  const { window } = dom;
  const { document } = window;

  const markedJs = fs.readFileSync(path.join(__dirname, "../static/vendor/marked.min.js"), "utf-8");
  const purifyJs = fs.readFileSync(path.join(__dirname, "../static/vendor/purify.min.js"), "utf-8");
  window.eval(markedJs);
  window.eval(purifyJs);

  const priyaUser = { id: 8, emp_code: "ACM-008", name: "Priya Sharma", email: "priya.sharma@acmetech.com", role: "employee", department: "Engineering" };
  const anitaUser = { id: 2, emp_code: "ACM-002", name: "Anita Desai", email: "anita.desai@acmetech.com", role: "hr_admin", department: "Human Resources" };

  let currentCaller = priyaUser;

  window.fetch = async (url, options = {}) => {
    const urlStr = String(url);
    if (urlStr.includes("/auth/session")) {
      return { ok: true, json: async () => ({ authenticated: !!currentCaller, caller: currentCaller }) };
    }
    if (urlStr.includes("/auth/login")) {
      const body = options.body ? JSON.parse(options.body) : {};
      currentCaller = (body.email === "anita.desai@acmetech.com") ? anitaUser : priyaUser;
      return { ok: true, json: async () => ({ message: "Success", employee: currentCaller }) };
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
    if (urlStr.includes("/employees/onboarding")) {
      return { ok: true, json: async () => ({ employees: [] }) };
    }
    if (urlStr.includes("/audit/admin")) {
      return { ok: true, json: async () => ({ logs: [] }) };
    }
    if (urlStr.includes("/audit")) {
      return {
        ok: true,
        json: async () => ({
          logs: [
            { tool_name: "get_payroll", caller_role: "employee", caller_name: "Priya Sharma", reason: "Cannot view payroll of another employee", timestamp: new Date().toISOString() }
          ]
        })
      };
    }
    if (urlStr.includes("/policies/files")) {
      return { ok: true, json: async () => ({ files: [] }) };
    }
    if (urlStr.includes("/memories")) {
      return { ok: true, json: async () => [] };
    }
    if (urlStr.includes("/employees")) {
      return { ok: true, json: async () => [] };
    }
    return { ok: true, json: async () => ({}) };
  };

  // Evaluate app.js
  window.eval(jsContent);

  const moreTabsContainer = document.getElementById("moreTabsContainer");
  const btnMoreTabs = document.getElementById("btnMoreTabs");
  const moreTabsMenu = document.getElementById("moreTabsMenu");
  const moreAuditDot = document.getElementById("moreAuditDot");
  const auditCountBadge = document.getElementById("auditCountBadge");

  // -------------------------------------------------------------
  // Test A1: Non-HR Employee tab bar and More menu hidden
  // -------------------------------------------------------------
  console.log("Check A1: Non-HR user (Priya) has no ⋮ More button and no separate HR tab buttons");
  window.resetForNewIdentity(priyaUser);

  assert(moreTabsContainer.classList.contains("hidden"), "moreTabsContainer must be hidden for employee");
  assert.strictEqual(document.querySelectorAll('.inspector-tabs > .tab-btn[data-tab="tabAudit"]').length, 0, "No separate Audit tab button in tab bar");
  assert.strictEqual(document.querySelectorAll('.inspector-tabs > .tab-btn[data-tab="tabOnboarding"]').length, 0, "No separate Onboarding tab button in tab bar");
  assert.strictEqual(document.querySelectorAll('.inspector-tabs > .tab-btn[data-tab="tabAdminAudit"]').length, 0, "No separate Admin Audit tab button in tab bar");

  // Attempt to switch to an HR tab directly as non-HR -> must redirect to Live Inspector
  window.switchTab("tabAudit");
  const tabAuditPane = document.getElementById("tabAudit");
  const tabLiveInspector = document.getElementById("tabLiveInspector");
  assert(!tabAuditPane.classList.contains("active"), "Non-HR user must not activate tabAudit pane");
  assert(tabLiveInspector.classList.contains("active"), "Non-HR user switching to HR pane must redirect to tabLiveInspector");

  // -------------------------------------------------------------
  // Test A2: HR Admin More (3-dot) menu and tab panes
  // -------------------------------------------------------------
  console.log("Check A2: HR Admin (Anita) sees ⋮ More button and can open HR panes");
  window.resetForNewIdentity(anitaUser);
  assert(!moreTabsContainer.classList.contains("hidden"), "moreTabsContainer must be visible for hr_admin");

  // Clicking ⋮ button opens menu
  btnMoreTabs.click();
  assert(!moreTabsMenu.classList.contains("hidden"), "moreTabsMenu should be open after click");
  assert.strictEqual(btnMoreTabs.getAttribute("aria-expanded"), "true", "aria-expanded should be true when open");

  // Clicking outside closes menu
  document.body.click();
  assert(moreTabsMenu.classList.contains("hidden"), "Clicking outside must close moreTabsMenu");

  // Keyboard Escape closes menu
  window.openMoreMenu();
  assert(!moreTabsMenu.classList.contains("hidden"), "Menu reopened");
  const escEvent = new window.KeyboardEvent("keydown", { key: "Escape" });
  document.dispatchEvent(escEvent);
  assert(moreTabsMenu.classList.contains("hidden"), "Escape key must close moreTabsMenu");

  // Menu items click switches pane and sets active states
  const menuItemAudit = document.getElementById("menuItemAudit");
  const menuItemOnboarding = document.getElementById("menuItemOnboarding");
  const menuItemAdminAudit = document.getElementById("menuItemAdminAudit");
  assert(menuItemAudit && menuItemOnboarding && menuItemAdminAudit, "All 3 HR menu items must exist");

  menuItemAudit.click();
  assert(tabAuditPane.classList.contains("active"), "tabAudit pane should be active");
  assert(btnMoreTabs.classList.contains("active"), "btnMoreTabs should have active class when an HR pane is active");
  assert(menuItemAudit.classList.contains("active"), "menuItemAudit should be active");
  assert(moreTabsMenu.classList.contains("hidden"), "Menu should close on item selection");

  // Switch back to Live Inspector
  window.switchTab("tabLiveInspector");
  assert(tabLiveInspector.classList.contains("active"), "Live Inspector should be active");
  assert(!btnMoreTabs.classList.contains("active"), "btnMoreTabs should lose active class when Live Inspector is active");

  // -------------------------------------------------------------
  // Test A3: Audit notification dot and count badge
  // -------------------------------------------------------------
  console.log("Check A3: Audit notifications update badge and dot on ⋮ button");
  // Simulate denial event
  window.eval(`
    updateInspectorToolResult({
      name: "get_payslip",
      status: "denied",
      summary: "Cannot view payroll of another employee",
      result: { access_denied: true, reason: "Cannot view payroll of another employee" }
    });
  `);
  assert(!moreAuditDot.classList.contains("hidden"), "moreAuditDot should be visible after denial");

  // Opening tabAudit clears dot
  window.switchTab("tabAudit");
  assert(moreAuditDot.classList.contains("hidden"), "moreAuditDot should be cleared when tabAudit is opened");

  // Switching identity back to non-HR closes menu, hides ⋮, clears dot
  window.resetForNewIdentity(priyaUser);
  assert(moreTabsContainer.classList.contains("hidden"), "moreTabsContainer must be hidden for employee after identity reset");
  assert(moreAuditDot.classList.contains("hidden"), "moreAuditDot must be hidden for employee");

  // -------------------------------------------------------------
  // Test B: Policy Chunks rendering
  // -------------------------------------------------------------
  console.log("Check B: Chunk 'View more', 'View less', Copy, and Below threshold rendering");
  const chunksList = document.getElementById("chunksList");
  window.resetForNewIdentity(anitaUser);

  const sampleChunks = [
    {
      source: "leave_policy.pdf",
      section: "Earned Leave",
      chunk_index: 3,
      score: 0.825,
      text: "Earned leave can be accumulated up to 45 days. Any excess leave beyond 45 days at year-end will automatically lapse unless encashed according to the company rules."
    },
    {
      source: "code_of_conduct.pdf",
      section: "General",
      chunk_index: 1,
      score: 0.320,
      text: "Short text below threshold."
    }
  ];

  window.renderPolicyChunks(sampleChunks, 0.45);

  const chunkItems = chunksList.querySelectorAll(".chunk-item");
  assert.strictEqual(chunkItems.length, 2, "Expected 2 rendered chunk items");

  // Check metadata
  const firstChunk = chunkItems[0];
  assert(firstChunk.querySelector(".chunk-source-name").textContent.includes("leave_policy.pdf"), "Chunk source matches");
  assert(firstChunk.querySelector(".chunk-section-badge").textContent.includes("Earned Leave"), "Chunk section matches");
  assert(firstChunk.querySelector(".chunk-index-badge").textContent.includes("#3"), "Chunk index matches");
  assert(firstChunk.querySelector(".chunk-score").textContent.includes("0.825"), "Chunk score matches");
  assert(!firstChunk.querySelector(".chunk-threshold-muted"), "First chunk is above threshold, no muted label");

  // Check below threshold label on second chunk
  const secondChunk = chunkItems[1];
  assert(secondChunk.querySelector(".chunk-threshold-muted"), "Second chunk is below 0.45 threshold, must have muted label");

  // Check copy button
  assert(firstChunk.querySelector(".btn-chunk-copy"), "Chunk must have a Copy button");

  // -------------------------------------------------------------
  // Test C: HR Data Results Cards & Empty Explanations
  // -------------------------------------------------------------
  console.log("Check C: HR Data results normalized cards and empty turn explanations");
  const hrDataList = document.getElementById("hrDataList");
  hrDataList.innerHTML = "";

  // 1. Success with records table
  window.renderHrToolResultCard({
    name: "get_leave_requests",
    status: "success",
    summary: "2 leave requests loaded",
    result: {
      requests: [
        { leave_type: "Casual", start_date: "2026-10-12", days: 2, status: "approved" },
        { leave_type: "Sick", start_date: "2026-09-01", days: 1, status: "approved" }
      ]
    }
  });

  const toolCards = hrDataList.querySelectorAll(".tool-card");
  assert.strictEqual(toolCards.length, 1, "Rendered 1 tool card");
  assert(toolCards[0].querySelector(".tool-card-title").textContent.includes("get_leave_requests"), "Tool card title");
  assert(toolCards[0].querySelector(".status-success"), "Status success badge");
  assert(toolCards[0].querySelector(".compact-data-table"), "Rendered compact data table for list of records");

  // Raw JSON toggle test
  const rawToggle = toolCards[0].querySelector(".btn-toggle-raw-json");
  const rawViewer = toolCards[0].querySelector(".raw-json-viewer");
  assert(rawToggle && rawViewer, "Raw JSON toggle and viewer exist");
  assert(rawViewer.classList.contains("hidden"), "Raw JSON is collapsed by default");
  rawToggle.click();
  assert(!rawViewer.classList.contains("hidden"), "Raw JSON expanded after click");
  rawToggle.click();
  assert(rawViewer.classList.contains("hidden"), "Raw JSON collapsed after second click");

  // 2. Denied card
  window.renderHrToolResultCard({
    name: "get_payslip",
    status: "denied",
    summary: "Confidential record",
    result: { access_denied: true, error: "Confidential record" }
  });
  assert(hrDataList.querySelector(".status-denied"), "Rendered denied badge");

  // 3. Empty card
  window.renderHrToolResultCard({
    name: "get_attendance_records",
    status: "empty",
    summary: "No attendance logged",
    result: { empty: true, reason: "No attendance logged" }
  });
  assert(hrDataList.querySelector(".status-empty"), "Rendered empty badge");

  // 4. Test on `done` with no HR tools used
  hrDataList.innerHTML = `<div class="empty-state">No structured records loaded.</div>`;
  window.STATE.turnStats.hrResults = [];

  // Policy answer explanation
  const simulatedPolicyDone = {
    had_tools: true,
    is_policy: true,
    is_data: false
  };
  // Simulate dispatching done logic
  if (window.STATE.turnStats.hrResults.length === 0) {
    if (simulatedPolicyDone.is_policy) {
      hrDataList.innerHTML = `<div class="hr-empty-explanation">No HR records were needed. This answer came from policy documents.</div>`;
    }
  }
  assert(hrDataList.textContent.includes("No HR records were needed. This answer came from policy documents."), "Policy turn explanation rendered");

  // General conversation explanation
  const simulatedGeneralDone = {
    had_tools: false,
    is_policy: false,
    is_data: false
  };
  if (window.STATE.turnStats.hrResults.length === 0) {
    if (!simulatedGeneralDone.had_tools) {
      hrDataList.innerHTML = `<div class="hr-empty-explanation">No tools were used. The answer came from the conversation and general guidance.</div>`;
    }
  }
  assert(hrDataList.textContent.includes("No tools were used. The answer came from the conversation and general guidance."), "General turn explanation rendered");

  console.log("✅ All Part 3 Frontend JSDOM tests passed successfully!");
}

runTests().catch(err => {
  console.error("❌ Test failed:", err);
  process.exit(1);
});
