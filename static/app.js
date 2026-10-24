/* Acme HRMS Assistant - Frontend JavaScript */

if (typeof marked !== "undefined" && marked.use) {
  marked.use({ breaks: true });
}

const SUGGEST_NEW_CHAT_AFTER = 3;
const SUGGEST_REPEAT_EVERY = 5;

const STATE = {
  activeUser: null,
  sessionId: "",
  allEmployees: [],
  isStreaming: false,
  chatAbortController: null,
  historyCount: 0,
  completedQueryCount: 0,
  turnStats: {
    tools: [],
    chunks: [],
    hrResults: [],
    denials: [],
    savedMemory: null
  }
};

const HR_TABS = ["tabAudit", "tabOnboarding", "tabAdminAudit"];

// Role-aware chips
const ROLE_CHIPS = {
  employee: [
    "How many casual leaves do I have left?",
    "What's the policy for carrying forward earned leave?",
    "What is the WFH policy?",
    "What is Rahul's salary?",
    "Show Rahul Mehta's payslip",
    "Show Anita's leave requests",
    "Show my attendance summary",
    "What upcoming holidays do we have?",
    "I prefer working from home on Fridays",
    "What's my WFH preference?"
  ],
  new_joiner: [
    "What do I do on Day 1?",
    "What documents do I need to submit?",
    "Who is my manager?",
    "How many leaves do I get this year?",
    "When is my first payday?",
    "How do I set up SSO / my laptop?",
    "Show my payslip",
    "Show my attendance summary"
  ],
  manager: [
    "Who in my team is on leave tomorrow?",
    "Who's on leave in my team this week?",
    "Show my own payslip for month 9 year 2026",
    "Show payslip for Priya Sharma",
    "What's the overtime and comp-off policy?",
    "Show my org chart"
  ],
  hr_admin: [
    "Can you give me the headcount statistics by department?",
    "Show Priya Sharma's leave history",
    "Show Rahul Mehta's profile",
    "What is the POSH complaint procedure?",
    "Show recent audit denials"
  ]
};

// DOM References
const badgeName = document.getElementById("badgeName");
const badgeRole = document.getElementById("badgeRole");
const badgeDept = document.getElementById("badgeDept");
const btnLogout = document.getElementById("btnLogout");

// Login Modal DOM
const loginModal = document.getElementById("loginModal");
const loginForm = document.getElementById("loginForm");
const loginEmail = document.getElementById("loginEmail");
const loginPassword = document.getElementById("loginPassword");
const loginAlert = document.getElementById("loginAlert");
const btnLoginSubmit = document.getElementById("btnLoginSubmit");

const sessionIdDisplay = document.getElementById("sessionIdDisplay");
const btnNewChat = document.getElementById("btnNewChat");
const btnClearScreen = document.getElementById("btnClearScreen");
const btnThemeToggle = document.getElementById("btnThemeToggle");
const themeIcon = document.getElementById("themeIcon");
const chatStream = document.getElementById("chatStream");
const chatForm = document.getElementById("chatForm");
const chatInput = document.getElementById("chatInput");
const btnSend = document.getElementById("btnSend");
const typingBar = document.getElementById("typingBar");
const typingStatus = document.getElementById("typingStatus");
const chipsList = document.getElementById("chipsList");
const toastContainer = document.getElementById("toastContainer");

// Inspector DOM
const mCallerRole = document.getElementById("mCallerRole");
const mHistoryCount = document.getElementById("mHistoryCount");
const mHistorySubtext = document.getElementById("mHistorySubtext");
const mToolCount = document.getElementById("mToolCount");

function setHistoryCount(n, details = null) {
  const count = typeof n === "number" ? n : (parseInt(n, 10) || 0);
  STATE.historyCount = count;
  if (mHistoryCount) {
    mHistoryCount.textContent = String(count);
  }
  if (!mHistorySubtext) return;

  if (count === 0 && !details) {
    mHistorySubtext.textContent = "";
    mHistorySubtext.className = "m-subtext";
    return;
  }

  if (details && details.history_omitted > 0) {
    mHistorySubtext.className = "m-subtext m-subtext-omitted";
    mHistorySubtext.innerHTML = `${escapeHtml(String(details.history_count))} of ${escapeHtml(String(details.history_total))} sent <span class="text-warning">(${escapeHtml(String(details.history_omitted))} oldest not sent)</span>`;
    return;
  }

  mHistorySubtext.className = "m-subtext";
  if (count === 0) {
    mHistorySubtext.textContent = details ? "0 messages" : "";
    return;
  }

  let u = details ? (details.history_user ?? details.userCount) : null;
  let a = details ? (details.history_assistant ?? details.assistantCount) : null;
  if (u === undefined || u === null || a === undefined || a === null) {
    u = Math.floor(count / 2);
    a = count - u;
  }
  const qWord = u === 1 ? "question" : "questions";
  const rWord = a === 1 ? "reply" : "replies";
  mHistorySubtext.textContent = `${u} ${qWord} + ${a} ${rWord}`;
}

// New Chat Suggestion DOM & Helpers
const newChatSuggestion = document.getElementById("newChatSuggestion");
const ncsText = document.getElementById("ncsText");
const btnNcsStart = document.getElementById("btnNcsStart");
const btnNcsKeep = document.getElementById("btnNcsKeep");
const btnNcsClose = document.getElementById("btnNcsClose");

function getCompletedQueryCount() {
  return STATE.completedQueryCount || 0;
}

function dismissNewChatSuggestion() {
  const count = STATE.completedQueryCount || 0;
  if (STATE.sessionId) {
    try {
      sessionStorage.setItem("hrms_dismiss_suggest_" + STATE.sessionId, String(count));
    } catch (e) {}
  }
  hideNewChatSuggestion();
}

function hideNewChatSuggestion() {
  if (newChatSuggestion) {
    newChatSuggestion.classList.add("hidden");
  }
}

function checkAndShowNewChatSuggestion() {
  if (!newChatSuggestion) return;
  const count = STATE.completedQueryCount || 0;
  if (count <= SUGGEST_NEW_CHAT_AFTER) {
    hideNewChatSuggestion();
    return;
  }

  if (STATE.isStreaming) {
    hideNewChatSuggestion();
    return;
  }

  if (STATE.sessionId) {
    try {
      const dismissedVal = sessionStorage.getItem("hrms_dismiss_suggest_" + STATE.sessionId);
      if (dismissedVal !== null) {
        const dismissedAt = parseInt(dismissedVal, 10);
        if (!isNaN(dismissedAt) && count < dismissedAt + SUGGEST_REPEAT_EVERY) {
          hideNewChatSuggestion();
          return;
        }
      }
    } catch (e) {}
  }

  if (ncsText) {
    ncsText.textContent = `This chat has ${count} questions. For a new topic, a new chat gives faster, more focused answers.`;
  }
  newChatSuggestion.classList.remove("hidden");
}

if (btnNcsStart) {
  btnNcsStart.addEventListener("click", () => startNewChat());
}
if (btnNcsKeep) {
  btnNcsKeep.addEventListener("click", () => dismissNewChatSuggestion());
}
if (btnNcsClose) {
  btnNcsClose.addEventListener("click", () => dismissNewChatSuggestion());
}
if (newChatSuggestion) {
  newChatSuggestion.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      dismissNewChatSuggestion();
    }
  });
}

const accessDeniedCard = document.getElementById("accessDeniedCard");
const accessDeniedContent = document.getElementById("accessDeniedContent");
const memorySavedCard = document.getElementById("memorySavedCard");
const memorySavedContent = document.getElementById("memorySavedContent");
const toolCallsList = document.getElementById("toolCallsList");
const chunksList = document.getElementById("chunksList");
const hrDataList = document.getElementById("hrDataList");
const auditDot = document.getElementById("auditDot");
const latencyBars = document.getElementById("latencyBars");
const timingTotalBadge = document.getElementById("timingTotalBadge");

// Tab & More Menu DOM
const moreTabsContainer = document.getElementById("moreTabsContainer");
const btnMoreTabs = document.getElementById("btnMoreTabs");
const moreTabsMenu = document.getElementById("moreTabsMenu");
const moreAuditDot = document.getElementById("moreAuditDot");
const auditCountBadge = document.getElementById("auditCountBadge");
const tabButtons = document.querySelectorAll(".tab-btn");
const tabPanes = document.querySelectorAll(".tab-pane");
const policyFilesList = document.getElementById("policyFilesList");
const userMemoriesList = document.getElementById("userMemoriesList");
const memoryUserName = document.getElementById("memoryUserName");
const btnResetMemories = document.getElementById("btnResetMemories");
const auditLogList = document.getElementById("auditLogList");
const btnRefreshAudit = document.getElementById("btnRefreshAudit");
const btnRefreshFiles = document.getElementById("btnRefreshFiles");
const btnDrawerAudit = document.getElementById("btnDrawerAudit");

// History DOM
const historyList = document.getElementById("historyList");
const btnRefreshHistory = document.getElementById("btnRefreshHistory");
const btnOpenHistory = document.getElementById("btnOpenHistory");
const tabBtnHistory = document.getElementById("tabBtnHistory");

// Upload DOM
const dropZone = document.getElementById("dropZone");
const policyUploadSection = document.getElementById("policyUploadSection");
const chkReplacePolicy = document.getElementById("chkReplacePolicy");
const policyFileInput = document.getElementById("policyFileInput");
const uploadProgress = document.getElementById("uploadProgress");
const uploadStatusText = document.getElementById("uploadStatusText");

// Initialize Session
function initSession() {
  if (window.crypto && typeof crypto.randomUUID === "function") {
    STATE.sessionId = crypto.randomUUID();
  } else {
    STATE.sessionId = 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
      const r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }
  if (sessionIdDisplay) sessionIdDisplay.textContent = STATE.sessionId;
}

// Toast helper
function showToast(message, type = "info") {
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  setTimeout(() => {
    toast.remove();
  }, 4500);
}

// Authentication Modal Helpers
function showLoginModal(alertMsg = "") {
  if (loginAlert) {
    if (alertMsg) {
      loginAlert.textContent = alertMsg;
      loginAlert.classList.remove("hidden");
    } else {
      loginAlert.classList.add("hidden");
    }
  }
  if (loginModal) loginModal.classList.remove("hidden");
}

function hideLoginModal() {
  if (loginModal) loginModal.classList.add("hidden");
  if (loginAlert) loginAlert.classList.add("hidden");
}

// Update UI for logged-in user
function updateUserUI(user) {
  if (!user) return;
  STATE.activeUser = user;
  if (badgeName) badgeName.textContent = user.name || "Employee";
  if (badgeRole) {
    badgeRole.textContent = user.role || "employee";
    badgeRole.className = `user-role role-${user.role || 'employee'}`;
  }
  if (badgeDept) badgeDept.textContent = user.department || "";
  if (mCallerRole) mCallerRole.textContent = user.role || "employee";
  if (memoryUserName) memoryUserName.textContent = user.name || "My";

  // Toggle HR More menu container visibility based on role
  if (moreTabsContainer) {
    if (user.role === "hr_admin") {
      moreTabsContainer.classList.remove("hidden");
    } else {
      moreTabsContainer.classList.add("hidden");
      closeMoreMenu();
      const activeHrPane = HR_TABS.find(id => {
        const p = document.getElementById(id);
        return p && p.classList.contains("active");
      });
      if (activeHrPane) {
        switchTab("tabLiveInspector");
      }
    }
  }

  if (user && user.must_change_password) {
    showChangePasswordModal();
  }
}

// Fetch all employees for user directory / manager pickers
async function loadEmployees() {
  try {
    const res = await fetch("/employees");
    if (res.status === 401) {
      showLoginModal("Please sign in to continue.");
      return;
    }
    if (res.ok) {
      const emps = await res.json();
      STATE.allEmployees = Array.isArray(emps) ? emps : [];
    }
  } catch (err) {
    console.warn("Could not populate employee directory:", err.message);
  }
}

// Render Welcome Card with 3 role/new-joiner chips
function renderWelcomeCard(user) {
  setHistoryCount(0);
  STATE.completedQueryCount = 0;
  hideNewChatSuggestion();
  chatStream.innerHTML = "";
  if (!user) return;
  const isNewJoiner = (user.status === "onboarding" || user.is_new_joiner);
  const roleKey = isNewJoiner ? "new_joiner" : (user.role || "employee");
  const chips = (ROLE_CHIPS[roleKey] || ROLE_CHIPS.employee).slice(0, 3);

  const welcomeEl = document.createElement("div");
  welcomeEl.className = "chat-message assistant-msg welcome-card";
  welcomeEl.innerHTML = `
    <div class="avatar assistant-avatar">🤖</div>
    <div class="msg-content">
      <h4>Welcome, ${escapeHtml(user.name)} (${escapeHtml(user.role || 'employee')})</h4>
      <p>${isNewJoiner ? "Welcome to Acme Technologies! Here are your Day 1 starting guides and workflows:" : "I can help you check your leave balance, file time-off queries, inspect attendance, view payslips, and review company policies."}</p>
      <div class="welcome-chips-quick">
        ${chips.map(c => `<button type="button" class="chip-btn welcome-chip">${escapeHtml(c)}</button>`).join("")}
      </div>
    </div>
  `;
  chatStream.appendChild(welcomeEl);

  welcomeEl.querySelectorAll(".welcome-chip").forEach(btn => {
    btn.addEventListener("click", () => {
      chatInput.value = btn.textContent;
      sendMessage();
    });
  });
}

// Global identity reset helper
function resetForNewIdentity(user) {
  setHistoryCount(0);
  STATE.completedQueryCount = 0;
  hideNewChatSuggestion();
  try {
    for (let i = sessionStorage.length - 1; i >= 0; i--) {
      const k = sessionStorage.key(i);
      if (k && k.startsWith("hrms_dismiss_suggest_")) {
        sessionStorage.removeItem(k);
      }
    }
  } catch (e) {}
  // 1. Abort any in-flight /chat request (on abort, do not show an error)
  if (STATE.chatAbortController) {
    try {
      STATE.chatAbortController.abort();
    } catch (e) {}
    STATE.chatAbortController = null;
  }

  // Reset inspector panel, typing indicator, streaming state
  STATE.isStreaming = false;
  if (btnSend) btnSend.disabled = false;
  if (typingBar) typingBar.classList.add("hidden");
  resetTurnStats();
  closeMoreMenu();
  if (auditLogList) auditLogList.innerHTML = `<div class="empty-state">No security access denials recorded.</div>`;
  if (auditDot) auditDot.classList.add("hidden");
  if (moreAuditDot) moreAuditDot.classList.add("hidden");
  if (auditCountBadge) {
    auditCountBadge.textContent = "0";
    auditCountBadge.classList.add("hidden");
  }

  // On logout / session-expired (user is null): clear chat DOM BEFORE showing modal
  if (!user) {
    chatStream.innerHTML = "";
    STATE.activeUser = null;
    initSession();
    if (badgeName) badgeName.textContent = "";
    if (badgeRole) {
      badgeRole.textContent = "";
      badgeRole.className = "user-role";
    }
    if (badgeDept) badgeDept.textContent = "";
    if (mCallerRole) mCallerRole.textContent = "";
    if (memoryUserName) memoryUserName.textContent = "My";
    if (moreTabsContainer) moreTabsContainer.classList.add("hidden");
    const activeHrPane = HR_TABS.find(id => {
      const p = document.getElementById(id);
      return p && p.classList.contains("active");
    });
    if (activeHrPane) {
      switchTab("tabLiveInspector");
    }
    const uploadEl = policyUploadSection || dropZone;
    if (uploadEl) uploadEl.classList.add("hidden");
    if (onboardingTableBody) onboardingTableBody.innerHTML = "";
    if (userMemoriesList) userMemoriesList.innerHTML = "";
    if (historyList) historyList.innerHTML = "";
    return;
  }

  // Update STATE.activeUser and UI header badges
  updateUserUI(user);

  // Generate a new session id with crypto.randomUUID()
  initSession();
  if (user.emp_code) {
    sessionStorage.setItem("hrms_active_session_" + user.emp_code, STATE.sessionId);
  }

  // Clear chat window and show welcome message for new user (name, role, 3 chips)
  renderWelcomeCard(user);

  // Reload everything that depends on the user
  renderRoleChips();
  loadUserMemories();
  loadPolicyFiles();
  loadEmployees();
  loadHistorySessions();

  const uploadEl = policyUploadSection || dropZone;
  if (user.role === "hr_admin") {
    if (uploadEl) uploadEl.classList.remove("hidden");
    if (moreTabsContainer) moreTabsContainer.classList.remove("hidden");
    loadAuditLogs();
    loadOnboardingEmployees();
    loadAdminAuditLogs();
  } else {
    if (uploadEl) uploadEl.classList.add("hidden");
    if (moreTabsContainer) moreTabsContainer.classList.add("hidden");
    const activeHrPane = HR_TABS.find(id => {
      const p = document.getElementById(id);
      return p && p.classList.contains("active");
    });
    if (activeHrPane) {
      switchTab("tabLiveInspector");
    }
    if (auditLogList) auditLogList.innerHTML = `<div class="empty-state">Audit logs are restricted to HR Administrators.</div>`;
    if (auditDot) auditDot.classList.add("hidden");
    if (moreAuditDot) moreAuditDot.classList.add("hidden");
    if (onboardingTableBody) onboardingTableBody.innerHTML = "";
    const adminAuditTableBody = document.getElementById("adminAuditTableBody");
    if (adminAuditTableBody) adminAuditTableBody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:14px;">Admin audit is restricted to HR Administrators.</td></tr>`;
  }
}

// Session Restore & History Logic
async function checkAndRestoreSession(user) {
  if (!user || !user.emp_code) return;
  try {
    const res = await fetch("/sessions?limit=20");
    if (res.status === 401) {
      handleSessionExpired();
      return;
    }
    if (!res.ok) return;
    const sessions = await res.json();
    renderHistoryList(sessions);

    if (!sessions || sessions.length === 0) {
      // No sessions exist; welcome message is already displayed by resetForNewIdentity
      return;
    }

    const savedSessionId = sessionStorage.getItem("hrms_active_session_" + user.emp_code);
    let targetSession = null;
    if (savedSessionId) {
      targetSession = sessions.find(s => s.session_id === savedSessionId);
    }
    if (!targetSession) {
      targetSession = sessions[0];
    }

    if (targetSession) {
      await loadSession(targetSession.session_id, targetSession, true);
    }
  } catch (err) {
    console.warn("Session restore failed:", err);
  }
}

async function loadSession(sessionId, sessionObj = null, showBanner = false) {
  if (STATE.chatAbortController) {
    try { STATE.chatAbortController.abort(); } catch (e) {}
    STATE.chatAbortController = null;
  }
  hideNewChatSuggestion();

  STATE.sessionId = sessionId;
  if (sessionIdDisplay) sessionIdDisplay.textContent = sessionId;
  if (STATE.activeUser && STATE.activeUser.emp_code) {
    sessionStorage.setItem("hrms_active_session_" + STATE.activeUser.emp_code, sessionId);
  }

  try {
    const res = await fetch(`/history?session_id=${encodeURIComponent(sessionId)}`);
    if (res.status === 401) {
      handleSessionExpired();
      return;
    }
    if (res.status === 403 || res.status === 404) {
      console.warn(`Session ${sessionId} returned ${res.status}. Falling back to fresh conversation.`);
      if (STATE.activeUser && STATE.activeUser.emp_code) {
        sessionStorage.removeItem("hrms_active_session_" + STATE.activeUser.emp_code);
      }
      initSession();
      if (STATE.activeUser) {
        renderWelcomeCard(STATE.activeUser);
      }
      loadHistorySessions();
      return;
    }
    if (!res.ok) throw new Error("Failed to load history");
    const history = await res.json();
    if (!Array.isArray(history) || history.length === 0) {
      setHistoryCount(0);
      STATE.completedQueryCount = 0;
      hideNewChatSuggestion();
    } else {
      const maxMsgs = (typeof window !== "undefined" && window.HISTORY_MAX_MESSAGES !== undefined)
        ? window.HISTORY_MAX_MESSAGES
        : (STATE.historyMaxMessages || 0);
      let countToSend = history.length;
      if (maxMsgs > 0 && countToSend > maxMsgs) {
        countToSend = maxMsgs;
      }
      const msgsToSend = history.slice(-countToSend);
      const u = msgsToSend.filter(m => m.role === "user").length;
      const a = msgsToSend.filter(m => m.role === "assistant").length;
      setHistoryCount(countToSend, { userCount: u, assistantCount: a });

      // Count completed user queries that got an assistant reply
      const completedReplies = history.filter(m => m.role === "assistant" && m.status !== "failed" && m.content && !m.content.includes("(failed)")).length;
      STATE.completedQueryCount = completedReplies;
      checkAndShowNewChatSuggestion();
    }

    chatStream.innerHTML = "";

    if (showBanner && history.length > 0) {
      const d = sessionObj && sessionObj.last_message_at
        ? new Date(sessionObj.last_message_at).toLocaleDateString()
        : (sessionObj && sessionObj.created_at ? new Date(sessionObj.created_at).toLocaleDateString() : "earlier");
      const bannerEl = document.createElement("div");
      bannerEl.className = "chat-restore-banner";
      bannerEl.innerHTML = `
        <span>Continuing your last conversation (${escapeHtml(d)})</span>
        <button type="button" class="btn-banner-new" id="btnBannerNewChat">Start new chat</button>
      `;
      chatStream.appendChild(bannerEl);
      const btnBannerNewChat = bannerEl.querySelector("#btnBannerNewChat");
      if (btnBannerNewChat) {
        btnBannerNewChat.addEventListener("click", () => startNewChat());
      }
    }

    history.forEach(msg => {
      if (msg.role === "user") {
        appendUserMessage(msg.content);
      } else if (msg.role === "assistant") {
        const body = createAssistantMessage();
        body.innerHTML = renderMarkdownSafely(msg.content);
      }
    });

    scrollChatToBottom();
    highlightActiveSession(sessionId);
  } catch (err) {
    console.warn("Failed to load session history:", err);
  }
}

function startNewChat() {
  if (STATE.chatAbortController) {
    try { STATE.chatAbortController.abort(); } catch (e) {}
    STATE.chatAbortController = null;
  }
  setHistoryCount(0);
  STATE.completedQueryCount = 0;
  hideNewChatSuggestion();
  if (STATE.sessionId) {
    try {
      sessionStorage.removeItem("hrms_dismiss_suggest_" + STATE.sessionId);
    } catch (e) {}
  }
  initSession();
  if (STATE.sessionId) {
    try {
      sessionStorage.removeItem("hrms_dismiss_suggest_" + STATE.sessionId);
    } catch (e) {}
  }
  if (STATE.activeUser && STATE.activeUser.emp_code) {
    sessionStorage.setItem("hrms_active_session_" + STATE.activeUser.emp_code, STATE.sessionId);
  }
  if (STATE.activeUser) {
    renderWelcomeCard(STATE.activeUser);
  } else {
    chatStream.innerHTML = "";
  }
  resetTurnStats();
  loadHistorySessions();
  showToast("Started fresh conversation session", "info");
}

async function loadHistorySessions() {
  if (!STATE.activeUser || !STATE.activeUser.emp_code) return;
  try {
    const res = await fetch("/sessions?limit=20");
    if (res.status === 401) return;
    if (!res.ok) return;
    const sessions = await res.json();
    renderHistoryList(sessions);
  } catch (e) {
    console.warn("Could not load history sessions:", e);
  }
}

function renderHistoryList(sessions) {
  if (!historyList) return;
  historyList.innerHTML = "";
  if (!sessions || sessions.length === 0) {
    historyList.innerHTML = `<div class="empty-state">No previous conversations recorded.</div>`;
    return;
  }

  sessions.forEach(s => {
    const item = document.createElement("div");
    item.className = `history-item ${s.session_id === STATE.sessionId ? 'active' : ''}`;
    item.dataset.id = s.session_id;

    const timeStr = s.last_message_at ? new Date(s.last_message_at).toLocaleString() : "";
    item.innerHTML = `
      <div class="history-item-content">
        <div class="history-item-title">${escapeHtml(s.title || 'Conversation')}</div>
        <div class="history-item-time">${escapeHtml(timeStr)}</div>
      </div>
      <button type="button" class="btn-delete-session" title="Delete conversation" data-id="${escapeHtml(s.session_id)}">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
      </button>
    `;

    const contentArea = item.querySelector(".history-item-content");
    contentArea.addEventListener("click", () => {
      loadSession(s.session_id, s, false);
      if (window.innerWidth <= 860) {
        closeInspector();
      }
    });

    const delBtn = item.querySelector(".btn-delete-session");
    delBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      await deleteSession(s.session_id);
    });

    historyList.appendChild(item);
  });
}

function highlightActiveSession(sessionId) {
  if (!historyList) return;
  historyList.querySelectorAll(".history-item").forEach(item => {
    if (item.dataset.id === sessionId) {
      item.classList.add("active");
    } else {
      item.classList.remove("active");
    }
  });
}

async function deleteSession(sessionId) {
  try {
    const res = await fetch(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
    if (res.status === 401) {
      handleSessionExpired();
      return;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to delete session");
    }

    showToast("Conversation deleted", "info");

    if (sessionId === STATE.sessionId) {
      startNewChat();
    } else {
      await loadHistorySessions();
    }
  } catch (err) {
    showToast(err.message, "error");
  }
}

function handleSessionExpired() {
  resetForNewIdentity(null);
  showLoginModal("Session expired. Please sign in again.");
}

// Render Role Chips
function renderRoleChips() {
  let role = (STATE.activeUser && STATE.activeUser.role) || "employee";
  if (STATE.activeUser && (STATE.activeUser.status === "onboarding" || STATE.activeUser.is_new_joiner)) {
    role = "new_joiner";
  }
  const chips = ROLE_CHIPS[role] || ROLE_CHIPS.employee;

  if (!chipsList) return;
  chipsList.innerHTML = "";
  chips.forEach(text => {
    const btn = document.createElement("button");
    btn.className = "chip-btn";
    btn.textContent = text;
    btn.type = "button";
    btn.addEventListener("click", () => {
      chatInput.value = text;
      sendMessage();
    });
    chipsList.appendChild(btn);
  });
}

// More HR Menu & Tab Switching Logic
function openMoreMenu() {
  if (!moreTabsMenu) return;
  moreTabsMenu.classList.remove("hidden");
  if (btnMoreTabs) {
    btnMoreTabs.setAttribute("aria-expanded", "true");
  }
}

function closeMoreMenu() {
  if (!moreTabsMenu) return;
  moreTabsMenu.classList.add("hidden");
  if (btnMoreTabs) {
    btnMoreTabs.setAttribute("aria-expanded", "false");
  }
}

function toggleMoreMenu() {
  if (!moreTabsMenu) return;
  if (moreTabsMenu.classList.contains("hidden")) {
    openMoreMenu();
  } else {
    closeMoreMenu();
  }
}

function switchTab(targetTab) {
  if (!targetTab) return;

  const isHrTab = HR_TABS.includes(targetTab);
  const isHrUser = Boolean(STATE.activeUser && STATE.activeUser.role === "hr_admin");

  // Non-HR users must never access HR panes
  if (isHrTab && !isHrUser) {
    targetTab = "tabLiveInspector";
  }

  // Deactivate all panes
  document.querySelectorAll(".tab-pane").forEach(p => p.classList.remove("active"));

  // Deactivate standard tab buttons
  document.querySelectorAll(".inspector-tabs .tab-btn:not(.btn-more-tabs)").forEach(b => {
    if (b.getAttribute("data-tab") === targetTab) {
      b.classList.add("active");
    } else {
      b.classList.remove("active");
    }
  });

  // Handle More (3-dot) button active state
  if (btnMoreTabs) {
    if (HR_TABS.includes(targetTab)) {
      btnMoreTabs.classList.add("active");
    } else {
      btnMoreTabs.classList.remove("active");
    }
  }

  // Handle more-menu-item active state
  document.querySelectorAll(".more-menu-item").forEach(item => {
    if (item.getAttribute("data-tab") === targetTab) {
      item.classList.add("active");
    } else {
      item.classList.remove("active");
    }
  });

  // Activate target pane
  const pane = document.getElementById(targetTab);
  if (pane) {
    pane.classList.add("active");
  }

  // Close more menu if open
  closeMoreMenu();

  // Clear unviewed audit alert if tabAudit opened
  if (targetTab === "tabAudit") {
    if (auditDot) auditDot.classList.add("hidden");
    if (moreAuditDot) moreAuditDot.classList.add("hidden");
  }

  // Lazy load pane data
  if (targetTab === "tabMemories") loadUserMemories();
  if (targetTab === "tabPolicies") loadPolicyFiles();
  if (targetTab === "tabAudit" && isHrUser) loadAuditLogs();
  if (targetTab === "tabOnboarding" && isHrUser) loadOnboardingEmployees();
  if (targetTab === "tabAdminAudit" && isHrUser) loadAdminAuditLogs();
  if (targetTab === "tabHistory") loadHistorySessions();
}

// Bind standard tab buttons
document.querySelectorAll(".inspector-tabs .tab-btn:not(.btn-more-tabs)").forEach(btn => {
  btn.addEventListener("click", () => {
    const targetTab = btn.getAttribute("data-tab");
    switchTab(targetTab);
  });
});

// More (3-dot) menu toggle and keyboard interactions
if (btnMoreTabs) {
  btnMoreTabs.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleMoreMenu();
  });

  btnMoreTabs.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") {
      e.preventDefault();
      openMoreMenu();
      const firstItem = moreTabsMenu ? moreTabsMenu.querySelector(".more-menu-item") : null;
      if (firstItem) firstItem.focus();
    }
  });
}

// Menu items interaction & keyboard navigation
document.querySelectorAll(".more-menu-item").forEach(item => {
  item.addEventListener("click", (e) => {
    e.stopPropagation();
    const tabId = item.getAttribute("data-tab");
    switchTab(tabId);
  });

  item.addEventListener("keydown", (e) => {
    const items = moreTabsMenu ? Array.from(moreTabsMenu.querySelectorAll(".more-menu-item")) : [];
    const idx = items.indexOf(item);

    if (e.key === "ArrowDown") {
      e.preventDefault();
      const next = items[(idx + 1) % items.length];
      if (next) next.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      const prev = items[(idx - 1 + items.length) % items.length];
      if (prev) prev.focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeMoreMenu();
      if (btnMoreTabs) btnMoreTabs.focus();
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      item.click();
    }
  });
});

// Close more menu when clicking outside
document.addEventListener("click", (e) => {
  if (moreTabsContainer && !moreTabsContainer.contains(e.target)) {
    closeMoreMenu();
  }
});

// Close more menu on Escape key anywhere
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && moreTabsMenu && !moreTabsMenu.classList.contains("hidden")) {
    closeMoreMenu();
    if (btnMoreTabs) btnMoreTabs.focus();
  }
});

const btnRefreshAdminAudit = document.getElementById("btnRefreshAdminAudit");
if (btnRefreshAdminAudit) {
  btnRefreshAdminAudit.addEventListener("click", loadAdminAuditLogs);
}

if (btnRefreshHistory) {
  btnRefreshHistory.addEventListener("click", loadHistorySessions);
}

if (btnOpenHistory) {
  btnOpenHistory.addEventListener("click", () => {
    switchTab("tabHistory");
    openInspector();
  });
}

// Theme Toggle
if (btnThemeToggle) {
  btnThemeToggle.addEventListener("click", () => {
    const current = document.body.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.body.setAttribute("data-theme", next);
    if (themeIcon) themeIcon.textContent = next === "dark" ? "☀️" : "🌙";
  });
}

// Chat Message Rendering
function appendUserMessage(text) {
  const msgEl = document.createElement("div");
  msgEl.className = "chat-message user-msg";
  const initial = STATE.activeUser ? STATE.activeUser.name.charAt(0) : "U";
  msgEl.innerHTML = `
    <div class="avatar user-avatar">${initial}</div>
    <div class="msg-content"><p>${escapeHtml(text)}</p></div>
  `;
  chatStream.appendChild(msgEl);
  scrollChatToBottom();
}

function createAssistantMessage() {
  const msgEl = document.createElement("div");
  msgEl.className = "chat-message assistant-msg";
  msgEl.innerHTML = `
    <div class="avatar assistant-avatar">🤖</div>
    <div class="msg-content">
      <div class="markdown-body"></div>
      <div class="msg-actions">
        <button class="btn-copy" title="Copy reply to clipboard">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>
          <span>Copy</span>
        </button>
      </div>
    </div>
  `;
  chatStream.appendChild(msgEl);
  scrollChatToBottom();

  const copyBtn = msgEl.querySelector(".btn-copy");
  copyBtn.addEventListener("click", () => {
    const rawText = msgEl.querySelector(".markdown-body").innerText;
    navigator.clipboard.writeText(rawText).then(() => {
      showToast("Copied reply to clipboard", "info");
    });
  });

  return msgEl.querySelector(".markdown-body");
}

function scrollChatToBottom() {
  chatStream.scrollTop = chatStream.scrollHeight;
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Fail-closed safe markdown rendering
function renderMarkdownSafely(text) {
  if (!text) return "";
  if (typeof DOMPurify !== "undefined" && DOMPurify && typeof DOMPurify.sanitize === "function") {
    const parsed = (typeof marked !== "undefined" && marked && typeof marked.parse === "function")
      ? marked.parse(text)
      : escapeHtml(text);
    return DOMPurify.sanitize(parsed);
  }
  // FAIL-CLOSED: When DOMPurify is missing, NEVER insert unsanitized HTML!
  // Fall back to escapeHtml(text)
  return escapeHtml(text);
}

if (typeof window !== "undefined") {
  window.escapeHtml = escapeHtml;
  window.renderMarkdownSafely = renderMarkdownSafely;
}

function resetTurnStats() {
  STATE.turnStats = {
    tools: [],
    chunks: [],
    hrResults: [],
    denials: [],
    savedMemory: null
  };
  toolCallsList.innerHTML = `<div class="empty-state">No tool calls in this turn.</div>`;
  chunksList.innerHTML = `<div class="empty-state">No policy chunks retrieved yet.</div>`;
  hrDataList.innerHTML = `<div class="empty-state">No structured records loaded.</div>`;
  accessDeniedCard.classList.add("hidden");
  memorySavedCard.classList.add("hidden");
  mToolCount.textContent = "0";
  resetTimingBreakdown();
}

function resetTimingBreakdown() {
  if (timingTotalBadge) timingTotalBadge.textContent = "-- ms";
  if (latencyBars) {
    latencyBars.innerHTML = `<div class="empty-state">No timing data for this turn yet.</div>`;
  }
}

function updateInspectorTiming(timing) {
  if (!latencyBars || !timing) return;
  const latencyCard = document.getElementById("latencyCard");
  if (latencyCard) latencyCard.classList.remove("hidden");
  const total = timing.total_request || 1;
  if (timingTotalBadge) {
    timingTotalBadge.textContent = `${timing.total_request || 0} ms`;
  }

  const stages = [
    { name: "Auth & Pre-LLM Context", val: Math.max(timing.auth || 0, timing.history_load || 0), class: "fill-context" },
    { name: "Session Owner Check", val: timing.session_owner_check || 0, class: "fill-auth" },
    { name: "Time to First Token (TTFT)", val: timing.ttft || 0, class: "fill-ttft" },
    { name: "LLM Generation", val: timing.llm_total || 0, class: "fill-llm" },
  ];

  if (timing.policy_search && timing.policy_search > 0) {
    stages.splice(2, 0, { name: "Policy Vector Search", val: timing.policy_search, class: "fill-policy" });
  }

  if (timing.tools_total && timing.tools_total > 0) {
    stages.splice(2, 0, { name: "Tool Executions", val: timing.tools_total, class: "fill-tool" });
  }

  if (timing.db_writes && timing.db_writes > 0) {
    stages.push({ name: "DB Writes (Bg)", val: timing.db_writes, class: "fill-db" });
  }

  latencyBars.innerHTML = stages.map(s => {
    const pct = Math.min(Math.round((s.val / total) * 100), 100);
    return `
      <div class="latency-row">
        <div class="latency-header">
          <span>${escapeHtml(s.name)}</span>
          <span class="latency-val">${s.val} ms (${pct}%)</span>
        </div>
        <div class="latency-bar-track">
          <div class="latency-bar-fill ${s.class}" style="width: ${pct}%;"></div>
        </div>
      </div>
    `;
  }).join("");
}

function updateInspectorContext(ctx) {
  if (ctx.caller && ctx.caller.role) {
    mCallerRole.textContent = ctx.caller.role;
  }
  if (ctx.history_max_messages !== undefined) {
    STATE.historyMaxMessages = ctx.history_max_messages;
  }
  if (ctx.history_count !== undefined) {
    setHistoryCount(ctx.history_count, ctx);
  }
}

function updateInspectorToolCall(toolCall) {
  const empty = toolCallsList.querySelector(".empty-state");
  if (empty) empty.remove();

  STATE.turnStats.tools.push(toolCall);
  mToolCount.textContent = STATE.turnStats.tools.length;

  const item = document.createElement("div");
  item.className = "tool-call-item";
  item.id = `tc_${toolCall.tool_call_id || Math.random()}`;
  item.innerHTML = `
    <div class="tool-name">${escapeHtml(toolCall.name)}</div>
    <div class="tool-args">${escapeHtml(JSON.stringify(toolCall.arguments))}</div>
    <div class="tool-status pending">Status: executing...</div>
  `;
  toolCallsList.appendChild(item);
}

function fallbackCopy(text, btnEl) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    document.body.removeChild(ta);
    if (btnEl) {
      btnEl.textContent = "Copied!";
      setTimeout(() => { btnEl.textContent = "Copy"; }, 1500);
    }
  } catch (e) {
    console.warn("Fallback copy failed", e);
  }
}

function renderPolicyChunks(results, policyMinScore) {
  if (!chunksList || !Array.isArray(results)) return;
  const empty = chunksList.querySelector(".empty-state");
  if (empty) empty.remove();

  results.forEach(r => {
    const chunkEl = document.createElement("div");
    chunkEl.className = "chunk-item";

    const scoreNum = (typeof r.score === "number") ? r.score : parseFloat(r.score);
    const isBelowThreshold = (policyMinScore !== undefined && !isNaN(scoreNum)) ? (scoreNum < policyMinScore) : false;
    const scoreText = !isNaN(scoreNum) ? scoreNum.toFixed(3) : "N/A";
    const sectionText = r.section || "General";
    const chunkIdxText = (r.chunk_index !== undefined && r.chunk_index !== null) ? `#${r.chunk_index}` : "";

    const headerEl = document.createElement("div");
    headerEl.className = "chunk-header";
    headerEl.innerHTML = `
      <div class="chunk-title-group">
        <span class="chunk-source-name">${escapeHtml(r.source || "Policy Document")}</span>
        <span class="chunk-section-badge">${escapeHtml(sectionText)}</span>
        ${chunkIdxText ? `<span class="chunk-index-badge">${escapeHtml(chunkIdxText)}</span>` : ""}
      </div>
      <div class="chunk-score-group">
        <span class="chunk-score">score: ${escapeHtml(scoreText)}</span>
        ${isBelowThreshold ? `<span class="chunk-threshold-muted">below threshold</span>` : ""}
      </div>
    `;
    chunkEl.appendChild(headerEl);

    const snippetEl = document.createElement("div");
    snippetEl.className = "chunk-snippet";
    snippetEl.textContent = r.text || "";
    chunkEl.appendChild(snippetEl);

    const actionsEl = document.createElement("div");
    actionsEl.className = "chunk-actions";

    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "btn-chunk-copy";
    copyBtn.textContent = "Copy";
    copyBtn.addEventListener("click", () => {
      const textToCopy = r.text || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(textToCopy).then(() => {
          copyBtn.textContent = "Copied!";
          setTimeout(() => { copyBtn.textContent = "Copy"; }, 1500);
        }).catch(() => {
          fallbackCopy(textToCopy, copyBtn);
        });
      } else {
        fallbackCopy(textToCopy, copyBtn);
      }
    });

    actionsEl.appendChild(copyBtn);
    chunkEl.appendChild(actionsEl);

    chunksList.appendChild(chunkEl);

    // Clamp detection ONLY after DOM render
    if (snippetEl.scrollHeight > snippetEl.clientHeight + 1) {
      const moreBtn = document.createElement("button");
      moreBtn.type = "button";
      moreBtn.className = "btn-chunk-more";
      moreBtn.textContent = "View more";
      moreBtn.addEventListener("click", () => {
        const isExp = snippetEl.classList.toggle("expanded");
        moreBtn.textContent = isExp ? "View less" : "View more";
      });
      actionsEl.insertBefore(moreBtn, copyBtn);
    }
  });
}

function renderHrToolResultCard(toolResult) {
  if (!hrDataList) return;
  const empty = hrDataList.querySelector(".empty-state");
  if (empty) empty.remove();

  STATE.turnStats.hrResults.push(toolResult);

  const card = document.createElement("div");
  card.className = "tool-card";

  // Normalize status
  let status = (toolResult.status || "success").toLowerCase();
  if (status === "access_denied") status = "denied";
  if (!["success", "empty", "denied", "error"].includes(status)) {
    status = "success";
  }

  // Header
  const header = document.createElement("div");
  header.className = "tool-card-header";
  header.innerHTML = `
    <span class="tool-card-title">${escapeHtml(toolResult.name)}</span>
    <span class="tool-status-badge status-${status}">${escapeHtml(status)}</span>
  `;
  card.appendChild(header);

  // Summary
  if (toolResult.summary) {
    const sumEl = document.createElement("div");
    sumEl.className = "tool-summary-line";
    sumEl.textContent = toolResult.summary;
    card.appendChild(sumEl);
  }

  // Content body
  const res = toolResult.result;
  const bodyEl = document.createElement("div");
  bodyEl.className = "tool-card-body";

  if (status === "denied") {
    const reason = (res && (res.error || res.reason)) || toolResult.summary || "Access denied by HR security policy.";
    bodyEl.innerHTML = `<div class="alert-body" style="padding:6px 8px;background:var(--danger-light);border-radius:4px;">⚠️ ${escapeHtml(reason)}</div>`;
  } else if (status === "error") {
    const err = (res && (res.error || res.reason)) || toolResult.summary || "Error executing tool operation.";
    bodyEl.innerHTML = `<div class="alert-body" style="padding:6px 8px;background:var(--danger-light);border-radius:4px;">⚠️ ${escapeHtml(err)}</div>`;
  } else if (status === "empty") {
    const msg = (res && (res.reason || res.message)) || toolResult.summary || "No matching records found.";
    bodyEl.innerHTML = `<div style="font-size:0.75rem;color:var(--text-muted);font-style:italic;padding:6px 8px;background:var(--bg-card-subtle);border-radius:4px;">ℹ️ ${escapeHtml(msg)}</div>`;
  } else if (res && typeof res === "object") {
    // Detect array of records
    let records = null;
    if (Array.isArray(res)) {
      records = res;
    } else {
      for (const key of ["requests", "recent_entries", "payslips", "team_members_on_leave", "holidays", "employees", "direct_reports", "reviews", "peers", "items"]) {
        if (Array.isArray(res[key]) && res[key].length > 0) {
          records = res[key];
          break;
        }
      }
    }

    if (records && records.length > 0 && typeof records[0] === "object") {
      const tblContainer = document.createElement("div");
      tblContainer.className = "compact-data-table-container";
      const table = document.createElement("table");
      table.className = "compact-data-table";

      const sample = records[0];
      const allKeys = Object.keys(sample).filter(k => !k.startsWith("_") && typeof sample[k] !== "object");
      const colKeys = allKeys.slice(0, 4);

      let theadHtml = "<thead><tr>" + colKeys.map(k => `<th>${escapeHtml(k.replace(/_/g, " "))}</th>`).join("") + "</tr></thead>";
      let tbodyHtml = "<tbody>";
      records.slice(0, 6).forEach(row => {
        tbodyHtml += "<tr>" + colKeys.map(k => `<td>${escapeHtml(String(row[k] ?? ""))}</td>`).join("") + "</tr>";
      });
      tbodyHtml += "</tbody>";
      table.innerHTML = theadHtml + tbodyHtml;
      tblContainer.appendChild(table);

      if (records.length > 6) {
        const moreNote = document.createElement("div");
        moreNote.style.cssText = "font-size:0.68rem;color:var(--text-subtle);margin-top:2px;";
        moreNote.textContent = `... and ${records.length - 6} more records.`;
        tblContainer.appendChild(moreNote);
      }

      bodyEl.appendChild(tblContainer);
    } else {
      const kvGrid = document.createElement("div");
      kvGrid.className = "compact-kv-grid";

      let entries = [];
      if (res.balances && typeof res.balances === "object") {
        for (const [k, v] of Object.entries(res.balances)) {
          entries.push([`${k} balance`, `${v} days`]);
        }
      } else if (res.breakdown && typeof res.breakdown === "object") {
        for (const [k, v] of Object.entries(res.breakdown)) {
          entries.push([k, v]);
        }
      } else if (res.by_department && typeof res.by_department === "object") {
        for (const [k, v] of Object.entries(res.by_department)) {
          entries.push([k, `${v} employees`]);
        }
      } else {
        for (const [k, v] of Object.entries(res)) {
          if (typeof v !== "object" && !k.startsWith("_")) {
            entries.push([k.replace(/_/g, " "), v]);
          }
        }
      }

      if (entries.length > 0) {
        entries.slice(0, 8).forEach(([k, v]) => {
          const kEl = document.createElement("span");
          kEl.className = "kv-key";
          kEl.textContent = k;
          const vEl = document.createElement("span");
          vEl.className = "kv-val";
          vEl.textContent = String(v);
          kvGrid.appendChild(kEl);
          kvGrid.appendChild(vEl);
        });
        bodyEl.appendChild(kvGrid);
      }
    }
  }

  card.appendChild(bodyEl);

  // Raw JSON toggle & pre viewer
  const jsonToggle = document.createElement("button");
  jsonToggle.type = "button";
  jsonToggle.className = "btn-toggle-raw-json";
  jsonToggle.textContent = "{ } Show raw JSON";

  const jsonPre = document.createElement("pre");
  jsonPre.className = "raw-json-viewer hidden";
  jsonPre.textContent = JSON.stringify(res || {}, null, 2);

  jsonToggle.addEventListener("click", () => {
    const isHidden = jsonPre.classList.toggle("hidden");
    jsonToggle.textContent = isHidden ? "{ } Show raw JSON" : "{ } Hide raw JSON";
  });

  card.appendChild(jsonToggle);
  card.appendChild(jsonPre);

  hrDataList.appendChild(card);
}

function updateInspectorToolResult(toolResult) {
  const item = document.getElementById(`tc_${toolResult.tool_call_id}`);
  if (item) {
    const statusEl = item.querySelector(".tool-status");
    if (statusEl) {
      statusEl.className = `tool-status ${toolResult.status}`;
      statusEl.textContent = `Status: ${toolResult.status} — ${toolResult.summary || "Done"}`;
    }
  }

  if (toolResult.status === "denied" || (toolResult.result && toolResult.result.access_denied)) {
    accessDeniedCard.classList.remove("hidden");
    const reason = (toolResult.result && toolResult.result.reason) || toolResult.summary || "Access denied";
    accessDeniedContent.innerHTML = `
      <div style="font-weight: 600; margin-bottom: 2px;">${escapeHtml(toolResult.name)}</div>
      <div>${escapeHtml(reason)}</div>
    `;
    if (auditDot) auditDot.classList.remove("hidden");
    if (moreAuditDot) moreAuditDot.classList.remove("hidden");
    showToast(`Access Control: ${reason}`, "error");
    loadAuditLogs();
  }

  if (toolResult.name === "search_policies" && toolResult.result && toolResult.result.results) {
    renderPolicyChunks(toolResult.result.results, toolResult.policy_min_score);
  }

  if (!["search_policies", "search_memory", "save_memory"].includes(toolResult.name)) {
    renderHrToolResultCard(toolResult);
  }
}

function updateInspectorMemorySaved(mem) {
  memorySavedCard.classList.remove("hidden");
  memorySavedContent.innerHTML = `
    <span>"${escapeHtml(mem.fact)}"</span>
  `;
  showToast(`Saved preference to long-term memory: "${mem.fact}"`, "success");
  loadUserMemories();
}

function getToolStatusMessage(toolName, toolArgs) {
  if (toolName === "read_policy_document") {
    const docName = (toolArgs && (toolArgs.document || toolArgs.source)) || "";
    const docSuffix = docName ? ` of ${docName}` : "";
    if (toolArgs && toolArgs.start_page) {
      if (toolArgs.end_page && toolArgs.end_page !== toolArgs.start_page) {
        return `Reading pages ${toolArgs.start_page} to ${toolArgs.end_page}${docSuffix}...`;
      }
      return `Reading page ${toolArgs.start_page}${docSuffix}...`;
    }
    return docName ? `Reading ${docName}...` : "Opening policy document...";
  }
  if (toolName === "list_policy_documents") {
    return "Opening policy documents...";
  }

  const map = {
    "get_leave_balances": "Looking up your leave balance...",
    "get_upcoming_holidays": "Checking upcoming company holidays...",
    "get_payslip": "Retrieving your latest payslip details...",
    "get_attendance_records": "Checking attendance records...",
    "get_employee_profile": "Loading employee profile...",
    "get_team_leaves": "Checking who is on leave...",
    "search_policies": "Searching HR policy documents...",
    "search_memory": "Checking preferences...",
    "save_memory": "Saving employee preference...",
    "file_leave_request": "Submitting leave request...",
    "get_all_employees": "Looking up employee directory...",
    "update_employee_manager": "Updating employee manager...",
  };
  return map[toolName] || `Executing ${toolName.replace(/_/g, " ")}...`;
}

function getToolResultStatusMessage(toolName, toolResult) {
  if (toolName === "read_policy_document") {
    const docName = (toolResult && (toolResult.document || toolResult.source)) || "";
    const docSuffix = docName ? ` of ${docName}` : "";
    if (toolResult && toolResult.start_page && toolResult.total_pages) {
      return `Read page ${toolResult.start_page} of ${toolResult.total_pages}${docSuffix}...`;
    } else if (toolResult && toolResult.start_page) {
      return `Read page ${toolResult.start_page}${docSuffix}...`;
    }
    return docName ? `Read ${docName}...` : "Policy document opened, preparing answer...";
  }
  if (toolName === "list_policy_documents") {
    return "Policy documents listed, preparing answer...";
  }

  const map = {
    "get_leave_balances": "Leave balance retrieved, preparing answer...",
    "get_upcoming_holidays": "Holiday calendar loaded, preparing answer...",
    "get_payslip": "Payslip details loaded, preparing answer...",
    "get_attendance_records": "Attendance details loaded, preparing answer...",
    "get_employee_profile": "Profile details loaded, preparing answer...",
    "get_team_leaves": "Team leave status loaded, preparing answer...",
    "search_policies": "Policy context retrieved, preparing answer...",
    "search_memory": "Preferences retrieved, preparing answer...",
    "save_memory": "Preference saved, preparing answer...",
    "file_leave_request": "Leave request processed, preparing answer...",
  };
  return map[toolName] || "Synthesizing verified HR answer...";
}

// Send Message & Handle Streaming SSE (NO client-supplied user_id)
async function sendMessage(isRetry = false, retryText = null) {
  let text = "";
  if (isRetry) {
    text = (retryText || "").trim();
  } else {
    text = chatInput.value.trim();
  }
  if (!text || STATE.isStreaming) return;

  if (!isRetry) {
    chatInput.value = "";
    chatInput.style.height = "auto";
    appendUserMessage(text);
  }
  document.querySelectorAll(".btn-retry-query").forEach(btn => btn.remove());
  resetTurnStats();

  STATE.isStreaming = true;
  hideNewChatSuggestion();
  btnSend.disabled = true;
  typingBar.classList.remove("hidden");
  typingStatus.textContent = "Assistant is processing request...";

  STATE.chatAbortController = new AbortController();

  const assistantBody = createAssistantMessage();
  let fullMarkdown = "";
  let lastError = null;
  let receivedDone = false;

  try {
    const response = await fetch("/chat", {
      method: "POST",
      signal: STATE.chatAbortController.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: STATE.sessionId,
        message: text,
        retry: isRetry
      })
    });

    if (response.status === 401) {
      handleSessionExpired();
      return;
    }

    if (response.status === 403) {
      const errData = await response.json().catch(() => ({}));
      if ((errData.detail && errData.detail.code === "PASSWORD_CHANGE_REQUIRED") || (typeof errData.detail === "string" && errData.detail.includes("PASSWORD_CHANGE_REQUIRED"))) {
        showChangePasswordModal();
        throw new Error("Password change required before accessing HRMS.");
      }
      throw new Error(errData.detail || "Access forbidden.");
    }

    if (!response.ok) {
      throw new Error(`Server returned HTTP ${response.status}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop();

      for (const block of lines) {
        if (!block.trim()) continue;

        let eventType = "message";
        let eventData = null;

        const blockLines = block.split("\n");
        for (const line of blockLines) {
          if (line.startsWith("event: ")) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            try {
              eventData = JSON.parse(line.slice(6));
            } catch (e) {
              eventData = line.slice(6);
            }
          }
        }

        if (eventType === "context_info" && eventData) {
          updateInspectorContext(eventData);
        } else if (eventType === "tool_call" && eventData) {
          updateInspectorToolCall(eventData);
          typingStatus.textContent = getToolStatusMessage(eventData.name, eventData.arguments);
          typingBar.classList.remove("hidden");
        } else if (eventType === "tool_result" && eventData) {
          updateInspectorToolResult(eventData);
          typingStatus.textContent = getToolResultStatusMessage(eventData.name, eventData.result);
          typingBar.classList.remove("hidden");
        } else if (eventType === "token" && eventData) {
          fullMarkdown += eventData.token;
          assistantBody.innerHTML = renderMarkdownSafely(fullMarkdown);
          typingStatus.textContent = "Writing response...";
          typingBar.classList.remove("hidden");
          scrollChatToBottom();
        } else if (eventType === "memory_saved" && eventData) {
          updateInspectorMemorySaved(eventData);
        } else if (eventType === "timing" && eventData) {
          updateInspectorTiming(eventData);
        } else if (eventType === "done") {
          receivedDone = true;
          typingBar.classList.add("hidden");
          btnSend.disabled = false;
          loadUserMemories();

          // Between turns: show what NEXT query will send (the count of all messages currently in session)
          const maxMsgs = (typeof window !== "undefined" && window.HISTORY_MAX_MESSAGES !== undefined)
            ? window.HISTORY_MAX_MESSAGES
            : (STATE.historyMaxMessages || 0);
          let nextCount = (STATE.historyCount || 0) + 2;
          if (maxMsgs > 0 && nextCount > maxMsgs) {
            nextCount = maxMsgs;
          }
          const nextUser = Math.floor(nextCount / 2);
          const nextAsst = nextCount - nextUser;
          setHistoryCount(nextCount, { userCount: nextUser, assistantCount: nextAsst });

          // Completed user query: evaluate new chat suggestion
          STATE.completedQueryCount = (STATE.completedQueryCount || 0) + 1;
          checkAndShowNewChatSuggestion();

          if (hrDataList && STATE.turnStats.hrResults.length === 0) {
            if (lastError || (eventData && eventData.error)) {
              const errMsg = lastError || (eventData && eventData.error);
              hrDataList.innerHTML = `
                <div class="hr-empty-explanation text-warning" style="border-left-color: var(--danger);">
                  ⚠️ ${escapeHtml(errMsg)}
                  <div style="margin-top: 4px; font-size: 0.72rem; color: var(--text-muted);">Hint: Check parameters or query formulation.</div>
                </div>
              `;
            } else if (eventData && eventData.is_policy) {
              hrDataList.innerHTML = `
                <div class="hr-empty-explanation">
                  No HR records were needed. This answer came from policy documents.
                </div>
              `;
            } else if (!eventData || !eventData.had_tools) {
              hrDataList.innerHTML = `
                <div class="hr-empty-explanation">
                  No tools were used. The answer came from the conversation and general guidance.
                </div>
              `;
            } else {
              hrDataList.innerHTML = `
                <div class="hr-empty-explanation">
                  No HR records were needed. This answer came from policy documents.
                </div>
              `;
            }
          }
        } else if (eventType === "error" && eventData) {
          lastError = (typeof eventData === "object" ? eventData.message : eventData) || "Model rate limit or network error occurred";
          showToast(`Error: ${lastError}`, "error");
        }
      }
    }

    if (!receivedDone) {
      const errBox = document.createElement("div");
      errBox.className = "turn-error-box";
      errBox.style.marginTop = "6px";
      const errMsg = lastError || "Connection dropped or stream interrupted before response completed.";
      errBox.innerHTML = `
        <p class="text-danger" style="margin-bottom: 6px;">⚠️ ${escapeHtml(errMsg)}</p>
        <button type="button" class="btn-sm btn-outline btn-retry-query" style="font-size: 0.75rem; padding: 3px 8px;">
          ↻ Retry query
        </button>
      `;
      assistantBody.appendChild(errBox);
      const retryBtn = errBox.querySelector(".btn-retry-query");
      if (retryBtn) {
        retryBtn.addEventListener("click", () => {
          const card = assistantBody.closest(".chat-message");
          if (card) card.remove();
          sendMessage(true, text);
        });
      }
    }
  } catch (err) {
    if (err.name === "AbortError") {
      const card = assistantBody.closest(".chat-message");
      if (card) card.remove();
      return;
    }
    const errBox = document.createElement("div");
    errBox.className = "turn-error-box";
    errBox.style.marginTop = "6px";
    const errMsg = err.message || "Connection dropped or network error occurred.";
    errBox.innerHTML = `
      <p class="text-danger" style="margin-bottom: 6px;">⚠️ ${escapeHtml(errMsg)}</p>
      <button type="button" class="btn-sm btn-outline btn-retry-query" style="font-size: 0.75rem; padding: 3px 8px;">
        ↻ Retry query
      </button>
    `;
    assistantBody.appendChild(errBox);
    const retryBtn = errBox.querySelector(".btn-retry-query");
    if (retryBtn) {
      retryBtn.addEventListener("click", () => {
        const card = assistantBody.closest(".chat-message");
        if (card) card.remove();
        sendMessage(true, text);
      });
    }
    showToast(errMsg, "error");
  } finally {
    STATE.isStreaming = false;
    btnSend.disabled = false;
    STATE.chatAbortController = null;
    typingBar.classList.add("hidden");
    scrollChatToBottom();
    setTimeout(() => {
      scrollChatToBottom();
    }, 60);
    loadHistorySessions();
  }
}

// User Memories Tab (returns caller's own data, no user_id query param needed)
async function loadUserMemories() {
  try {
    const res = await fetch("/memories");
    if (res.status === 401) {
      handleSessionExpired();
      return;
    }
    if (!res.ok) throw new Error("Failed to fetch memories");
    const mems = await res.json();

    userMemoriesList.innerHTML = "";
    const name = STATE.activeUser ? STATE.activeUser.name : "you";
    if (!mems.length) {
      userMemoriesList.innerHTML = `<div class="empty-state">No durable preferences stored for ${escapeHtml(name)}.</div>`;
      return;
    }

    mems.forEach(m => {
      const el = document.createElement("div");
      el.className = "memory-item";
      el.innerHTML = `
        <div>${escapeHtml(m.text)}</div>
        <div class="memory-item-date">${m.created_at ? new Date(m.created_at).toLocaleString() : ""}</div>
      `;
      userMemoriesList.appendChild(el);
    });
  } catch (err) {
    userMemoriesList.innerHTML = `<div class="empty-state text-danger">${escapeHtml(err.message)}</div>`;
  }
}

btnResetMemories.addEventListener("click", async () => {
  const name = STATE.activeUser ? STATE.activeUser.name : "yourself";
  if (!confirm(`Clear all long-term memories for ${name}?`)) return;
  try {
    const res = await fetch("/memories", { method: "DELETE" });
    if (res.status === 401) {
      handleSessionExpired();
      return;
    }
    if (!res.ok) throw new Error("Failed to clear memories");
    showToast(`Cleared memories for ${name}`, "info");
    loadUserMemories();
  } catch (err) {
    showToast(err.message, "error");
  }
});

// Safe response parser for HTTP responses: reads text, parses JSON, handles string/object/array detail, network errors, non-JSON bodies
async function parseResponsePayload(res) {
  let text = "";
  try {
    text = await res.text();
  } catch (err) {
    return { ok: false, status: res.status, error: "Network error: " + (err.message || String(err)), message: "Network error: " + (err.message || String(err)) };
  }

  let data = null;
  if (text && text.trim()) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      return { ok: res.ok, status: res.status, rawText: text, message: text };
    }
  }

  if (!data) {
    return { ok: res.ok, status: res.status, message: res.statusText || "Empty response" };
  }

  let message = "";
  let code = data.code || null;

  if (typeof data.message === "string" && data.message.trim()) {
    message = data.message.trim();
  } else if (typeof data.detail === "string" && data.detail.trim()) {
    message = data.detail.trim();
  } else if (data.detail && typeof data.detail === "object") {
    if (Array.isArray(data.detail)) {
      message = data.detail.map(d => (d.msg || (typeof d === "string" ? d : JSON.stringify(d)))).join("; ");
    } else if (typeof data.detail.message === "string") {
      message = data.detail.message;
      if (!code && data.detail.code) code = data.detail.code;
    } else {
      message = JSON.stringify(data.detail);
    }
  } else if (data.error && typeof data.error === "string") {
    message = data.error;
  } else if (data.error && typeof data.error === "object") {
    message = data.error.message || JSON.stringify(data.error);
  } else if (code) {
    message = code;
  } else {
    message = text || res.statusText || `Request failed with status ${res.status}`;
  }

  return {
    ok: res.ok,
    status: res.status,
    data: data,
    code: code,
    message: message,
  };
}

// Policies Tab: List & Upload
async function loadPolicyFiles() {
  if (!policyFilesList) return;
  try {
    const res = await fetch("/policies/files");
    if (res.status === 401) {
      handleSessionExpired();
      return;
    }
    const parsed = await parseResponsePayload(res);
    if (!res.ok) throw new Error(parsed.message || "Failed to fetch files");
    const data = parsed.data || {};

    policyFilesList.innerHTML = "";
    if (!data.files || !data.files.length) {
      policyFilesList.innerHTML = `<div class="empty-state">No policy documents found.</div>`;
      return;
    }

    const isHrAdmin = STATE.activeUser && STATE.activeUser.role === "hr_admin";

    data.files.forEach(f => {
      const el = document.createElement("div");
      el.className = "policy-file-item";

      const fileName = f.name || f.source || "Unknown";
      const chunksStr = (f.chunks !== undefined && f.chunks !== null) ? `${f.chunks} chunks` : "";
      const pagesStr = (f.pages !== undefined && f.pages !== null) ? `${f.pages} pages` : "";
      const metaParts = [];
      if (chunksStr) metaParts.push(chunksStr);
      if (pagesStr) metaParts.push(pagesStr);
      if (f.uploaded_at) {
        try {
          metaParts.push(new Date(f.uploaded_at).toLocaleDateString());
        } catch (e) {
          metaParts.push(String(f.uploaded_at));
        }
      }
      const metaStr = metaParts.join(" · ");

      let deleteBtnHtml = "";
      if (isHrAdmin) {
        deleteBtnHtml = `
          <button type="button" class="btn-delete-policy" title="Delete policy" data-source="${escapeHtml(f.source || f.name)}" style="background:none;border:none;cursor:pointer;color:var(--danger);padding:4px;display:flex;align-items:center;">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
          </button>
        `;
      }

      el.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:2px;overflow:hidden;flex:1;min-width:0;">
          <span class="policy-file-name" style="word-break:break-all;">${escapeHtml(fileName)}</span>
          <span class="policy-file-size" style="font-size:0.72rem;color:var(--text-subtle);">${escapeHtml(metaStr)}</span>
        </div>
        ${deleteBtnHtml}
      `;

      if (isHrAdmin) {
        const btnDel = el.querySelector(".btn-delete-policy");
        if (btnDel) {
          btnDel.addEventListener("click", async (e) => {
            e.stopPropagation();
            const source = btnDel.dataset.source;
            const ok = window.confirm(`Are you sure you want to delete policy document "${source}"?`);
            if (!ok) return;

            btnDel.disabled = true;
            try {
              const delRes = await fetch(`/policies/files/${encodeURIComponent(source)}`, {
                method: "DELETE",
              });
              if (delRes.status === 401) {
                handleSessionExpired();
                return;
              }
              const delParsed = await parseResponsePayload(delRes);
              if (!delRes.ok) {
                throw new Error(delParsed.message || "Failed to delete policy");
              }
              showToast(`Policy document "${source}" deleted`, "info");
              await loadPolicyFiles();
            } catch (delErr) {
              showToast(`Delete error: ${delErr.message}`, "error");
            } finally {
              btnDel.disabled = false;
            }
          });
        }
      }

      policyFilesList.appendChild(el);
    });
  } catch (err) {
    policyFilesList.innerHTML = `<div class="empty-state text-danger">${escapeHtml(err.message)}</div>`;
  }
}

if (dropZone) {
  dropZone.addEventListener("click", () => {
    if (dropZone.classList.contains("disabled")) return;
    if (policyFileInput) policyFileInput.click();
  });
}
if (policyFileInput) {
  policyFileInput.addEventListener("change", (e) => {
    if (e.target.files.length) uploadFile(e.target.files[0]);
  });
}

if (dropZone) {
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    if (!dropZone.classList.contains("disabled")) {
      dropZone.classList.add("dragover");
    }
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("dragover");
  });

  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragover");
    if (dropZone.classList.contains("disabled")) return;
    if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
  });
}

async function uploadFile(file) {
  if (!file) return;

  const replaceChecked = chkReplacePolicy && chkReplacePolicy.checked;
  const url = replaceChecked ? "/ingest?replace=true" : "/ingest";

  // Disable drop zone while uploading
  if (dropZone) {
    dropZone.classList.add("disabled");
    dropZone.style.pointerEvents = "none";
    dropZone.style.opacity = "0.5";
  }
  if (chkReplacePolicy) chkReplacePolicy.disabled = true;
  if (policyFileInput) policyFileInput.disabled = true;

  if (uploadProgress) uploadProgress.classList.remove("hidden");
  if (uploadStatusText) uploadStatusText.textContent = "Uploading...";

  let timerExtract = setTimeout(() => {
    if (uploadStatusText) {
      uploadStatusText.textContent = "Extracting and embedding (this can take a minute for large PDFs)...";
    }
  }, 1000);

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res = await fetch(url, {
      method: "POST",
      body: formData,
    });

    if (res.status === 401) {
      handleSessionExpired();
      return;
    }

    const parsed = await parseResponsePayload(res);

    if (!res.ok) {
      const typedCodes = ["PDF_NO_TEXT", "PDF_ENCRYPTED", "PDF_CORRUPT", "TOO_LARGE", "QDRANT_TIMEOUT"];
      const isTypedError = (parsed.code && typedCodes.includes(parsed.code)) ||
                           res.status === 422 || res.status === 413 || res.status === 504 ||
                           typedCodes.some(c => (parsed.message || "").includes(c));

      if (isTypedError) {
        showToast(parsed.message || "Document ingestion issue", "warning");
      } else {
        showToast(parsed.message || "Upload failed", "error");
      }
      return;
    }

    const result = parsed.data || {};
    const chunks = result.chunks !== undefined && result.chunks !== null ? result.chunks : (result.chunks_ingested || 0);
    const pages = result.pages !== undefined && result.pages !== null ? result.pages : 1;

    showToast(`${chunks} chunks from ${pages} pages`, "success");
    await loadPolicyFiles();
  } catch (err) {
    showToast(`Upload error: ${err.message || String(err)}`, "error");
  } finally {
    clearTimeout(timerExtract);
    if (uploadProgress) uploadProgress.classList.add("hidden");
    if (dropZone) {
      dropZone.classList.remove("disabled");
      dropZone.style.pointerEvents = "";
      dropZone.style.opacity = "";
    }
    if (chkReplacePolicy) chkReplacePolicy.disabled = false;
    if (policyFileInput) {
      policyFileInput.disabled = false;
      policyFileInput.value = "";
    }
  }
}

async function loadAuditLogs() {
  if (!STATE.activeUser || STATE.activeUser.role !== "hr_admin") {
    if (auditLogList) auditLogList.innerHTML = `<div class="empty-state">Audit logs are restricted to HR Administrators.</div>`;
    if (auditDot) auditDot.classList.add("hidden");
    if (moreAuditDot) moreAuditDot.classList.add("hidden");
    if (auditCountBadge) { auditCountBadge.textContent = "0"; auditCountBadge.classList.add("hidden"); }
    return;
  }

  try {
    const res = await fetch("/audit");
    if (res.status === 401 || res.status === 403) {
      if (auditLogList) auditLogList.innerHTML = `<div class="empty-state">Audit logs are restricted to HR Administrators.</div>`;
      if (auditDot) auditDot.classList.add("hidden");
      if (moreAuditDot) moreAuditDot.classList.add("hidden");
      if (auditCountBadge) { auditCountBadge.textContent = "0"; auditCountBadge.classList.add("hidden"); }
      return;
    }
    if (!res.ok) throw new Error("Failed to fetch audit log");
    const data = await res.json();

    if (auditLogList) auditLogList.innerHTML = "";
    if (!data.logs || !data.logs.length) {
      if (auditLogList) auditLogList.innerHTML = `<div class="empty-state">No security access denials recorded.</div>`;
      if (auditDot) auditDot.classList.add("hidden");
      if (moreAuditDot) moreAuditDot.classList.add("hidden");
      if (auditCountBadge) { auditCountBadge.textContent = "0"; auditCountBadge.classList.add("hidden"); }
      return;
    }

    if (auditCountBadge) {
      auditCountBadge.textContent = String(data.logs.length);
      auditCountBadge.classList.remove("hidden");
    }

    const auditPane = document.getElementById("tabAudit");
    const isAuditActive = auditPane && auditPane.classList.contains("active");
    if (!isAuditActive) {
      if (auditDot) auditDot.classList.remove("hidden");
      if (moreAuditDot) moreAuditDot.classList.remove("hidden");
    }

    data.logs.forEach(log => {
      const el = document.createElement("div");
      el.className = "audit-item";
      el.innerHTML = `
        <div class="audit-header">
          <span class="audit-tool">${escapeHtml(log.tool_name)}</span>
          <span>${escapeHtml(log.caller_role)} (${escapeHtml(log.caller_name)})</span>
        </div>
        <div class="audit-reason">${escapeHtml(log.reason)}</div>
        <div style="font-size: 0.68rem; color: var(--text-subtle); margin-top: 2px;">
          ${log.timestamp ? new Date(log.timestamp).toLocaleTimeString() : ""} · Target: ${escapeHtml(log.target_info || "N/A")}
        </div>
      `;
      if (auditLogList) auditLogList.appendChild(el);
    });
  } catch (err) {
    if (auditLogList) auditLogList.innerHTML = `<div class="empty-state text-danger">${escapeHtml(err.message)}</div>`;
  }
}

// Login Form Submit Event
if (loginForm) {
  loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginAlert.classList.add("hidden");
    loginAlert.textContent = "";
    btnLoginSubmit.disabled = true;
    btnLoginSubmit.textContent = "Signing in...";

    const email = loginEmail.value.trim();
    const password = loginPassword.value;

    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password })
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Invalid login credentials");
      }

      resetForNewIdentity(data.employee);
      hideLoginModal();
      loginPassword.value = "";
      showToast(`Welcome back, ${data.employee.name}!`, "success");
      await checkAndRestoreSession(data.employee);
    } catch (err) {
      loginAlert.textContent = err.message;
      loginAlert.classList.remove("hidden");
    } finally {
      btnLoginSubmit.disabled = false;
      btnLoginSubmit.textContent = "Sign In";
    }
  });
}

// Logout Button Event
if (btnLogout) {
  btnLogout.addEventListener("click", async () => {
    resetForNewIdentity(null);
    showLoginModal();
    try {
      await fetch("/auth/logout", { method: "POST" });
    } catch (e) {}
    showToast("You have been signed out.", "info");
  });
}

btnNewChat.addEventListener("click", () => {
  startNewChat();
});

btnClearScreen.addEventListener("click", () => {
  chatStream.innerHTML = "";
});

btnRefreshAudit.addEventListener("click", loadAuditLogs);
btnRefreshFiles.addEventListener("click", loadPolicyFiles);

// Inspector Toggle and Drawer handling
const btnToggleInspector = document.getElementById("btnToggleInspector");
const btnCloseInspector = document.getElementById("btnCloseInspector");
const inspectorSection = document.querySelector(".inspector-section");
const inspectorBackdrop = document.getElementById("inspectorBackdrop");

function openInspector() {
  if (inspectorSection) inspectorSection.classList.add("open");
  if (btnToggleInspector) btnToggleInspector.classList.add("active");
  if (inspectorBackdrop) inspectorBackdrop.classList.add("open");
}

function closeInspector() {
  if (inspectorSection) inspectorSection.classList.remove("open");
  if (btnToggleInspector) btnToggleInspector.classList.remove("active");
  if (inspectorBackdrop) inspectorBackdrop.classList.remove("open");
}

if (btnDrawerAudit) {
  btnDrawerAudit.addEventListener("click", () => {
    switchTab("tabAudit");
    openInspector();
  });
}

if (btnToggleInspector && inspectorSection) {
  btnToggleInspector.addEventListener("click", () => {
    if (inspectorSection.classList.contains("open")) {
      closeInspector();
    } else {
      openInspector();
    }
  });
}

if (btnCloseInspector) {
  btnCloseInspector.addEventListener("click", closeInspector);
}

if (inspectorBackdrop) {
  inspectorBackdrop.addEventListener("click", closeInspector);
}

// Auto-expand textarea on typing & Enter key to send
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

chatInput.addEventListener("input", () => {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + "px";
});

chatForm.addEventListener("submit", (e) => {
  e.preventDefault();
  sendMessage();
});

// ---- HR ONBOARDING & ACCOUNT ACTIVATION LOGIC ----

const addEmployeeForm = document.getElementById("addEmployeeForm");
const onboardingTableBody = document.getElementById("onboardingTableBody");
const btnRefreshOnboarding = document.getElementById("btnRefreshOnboarding");
const inviteResultCard = document.getElementById("inviteResultCard");
const inviteLinkInput = document.getElementById("inviteLinkInput");
const btnCopyInvite = document.getElementById("btnCopyInvite");
const btnOpenActivate = document.getElementById("btnOpenActivate");

// Activation Modal DOM
const activateModal = document.getElementById("activateModal");
const activateForm = document.getElementById("activateForm");
const activateToken = document.getElementById("activateToken");
const activatePassword = document.getElementById("activatePassword");
const activateConfirmPassword = document.getElementById("activateConfirmPassword");
const activateAlert = document.getElementById("activateAlert");
const strengthText = document.getElementById("strengthText");

function showActivateModal(token = "") {
  if (activateToken) activateToken.value = token;
  if (activateModal) activateModal.classList.remove("hidden");
  if (activateAlert) activateAlert.classList.add("hidden");
}

function hideActivateModal() {
  if (activateModal) activateModal.classList.add("hidden");
  if (activateAlert) activateAlert.classList.add("hidden");
}

// Password strength indicator
if (activatePassword) {
  activatePassword.addEventListener("input", () => {
    const pwd = activatePassword.value;
    if (pwd.length < 10) {
      strengthText.textContent = `Too short (${pwd.length}/10 chars). Minimum 10 characters required.`;
      strengthText.style.color = "var(--danger)";
    } else {
      strengthText.textContent = "Strong password length! Meets requirements.";
      strengthText.style.color = "var(--success)";
    }
  });
}

// Handle Activation Form Submission
if (activateForm) {
  activateForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const token = activateToken.value.trim();
    const pwd = activatePassword.value;
    const confirmPwd = activateConfirmPassword.value;

    if (pwd !== confirmPwd) {
      if (activateAlert) {
        activateAlert.textContent = "Passwords do not match.";
        activateAlert.classList.remove("hidden");
      }
      return;
    }

    try {
      const res = await fetch("/auth/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: pwd })
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Activation failed.");
      }

      hideActivateModal();
      showToast("Account activated successfully! Welcome to Acme.", "success");
      resetForNewIdentity(data.employee);
      await checkAndRestoreSession(data.employee);

      // Clear URL parameter if present
      if (window.history.replaceState) {
        window.history.replaceState({}, document.title, window.location.pathname);
      }
    } catch (err) {
      if (activateAlert) {
        activateAlert.textContent = err.message;
        activateAlert.classList.remove("hidden");
      }
    }
  });
}

// Change Password Modal DOM
const changePasswordModal = document.getElementById("changePasswordModal");
const changePasswordForm = document.getElementById("changePasswordForm");
const currentPasswordInput = document.getElementById("currentPassword");
const newPasswordInput = document.getElementById("newPassword");
const confirmNewPasswordInput = document.getElementById("confirmNewPassword");
const changePasswordAlert = document.getElementById("changePasswordAlert");
const btnChangePasswordSubmit = document.getElementById("btnChangePasswordSubmit");

function showChangePasswordModal() {
  if (changePasswordModal) changePasswordModal.classList.remove("hidden");
  if (changePasswordAlert) {
    changePasswordAlert.textContent = "";
    changePasswordAlert.classList.add("hidden");
  }
}

function hideChangePasswordModal() {
  if (changePasswordModal) changePasswordModal.classList.add("hidden");
  if (changePasswordAlert) changePasswordAlert.classList.add("hidden");
}

if (changePasswordForm) {
  changePasswordForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const current_password = currentPasswordInput.value;
    const new_password = newPasswordInput.value;
    const confirm_password = confirmNewPasswordInput.value;

    if (new_password !== confirm_password) {
      if (changePasswordAlert) {
        changePasswordAlert.textContent = "New passwords do not match.";
        changePasswordAlert.classList.remove("hidden");
      }
      return;
    }

    try {
      if (btnChangePasswordSubmit) {
        btnChangePasswordSubmit.disabled = true;
        btnChangePasswordSubmit.textContent = "Updating...";
      }
      const res = await fetch("/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ current_password, new_password })
      });
      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.detail || "Failed to change password.");
      }
      hideChangePasswordModal();
      currentPasswordInput.value = "";
      newPasswordInput.value = "";
      confirmNewPasswordInput.value = "";
      showToast("Password updated successfully!", "success");
    } catch (err) {
      if (changePasswordAlert) {
        changePasswordAlert.textContent = err.message;
        changePasswordAlert.classList.remove("hidden");
      }
    } finally {
      if (btnChangePasswordSubmit) {
        btnChangePasswordSubmit.disabled = false;
        btnChangePasswordSubmit.textContent = "Update Password";
      }
    }
  });
}

// Load Onboarding Employees (hr_admin only)
async function loadOnboardingEmployees() {
  if (!onboardingTableBody) return;
  try {
    const res = await fetch("/employees/onboarding");
    if (!res.ok) return;
    const data = await res.json();
    const emps = data.employees || [];

    if (emps.length === 0) {
      onboardingTableBody.innerHTML = `<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:14px;">No employees currently in onboarding status.</td></tr>`;
      return;
    }

    onboardingTableBody.innerHTML = "";
    emps.forEach(emp => {
      const tr = document.createElement("tr");
      const stateBadge = `<span class="badge-invite-${escapeHtml(emp.invite_state)}">${escapeHtml(emp.invite_state.replace("_", " "))}</span>`;
      const btnLabel = emp.invite_state === 'expired' ? 'Resend Invite' : (emp.invite_state === 'not_sent' ? 'Invite' : 'Re-Invite');
      tr.innerHTML = `
        <td><code>${escapeHtml(emp.emp_code)}</code></td>
        <td><strong>${escapeHtml(emp.name)}</strong><br><small style="color:var(--text-muted)">${escapeHtml(emp.email)}</small></td>
        <td>${escapeHtml(emp.department)}</td>
        <td>${escapeHtml(emp.join_date)}</td>
        <td>${stateBadge}</td>
        <td>
          <button class="btn-sm btn-outline btn-invite-emp" data-id="${escapeHtml(String(emp.id))}" data-name="${escapeHtml(emp.name)}">
            ${btnLabel}
          </button>
        </td>
      `;
      onboardingTableBody.appendChild(tr);
    });

    // Wire Invite buttons
    document.querySelectorAll(".btn-invite-emp").forEach(btn => {
      btn.addEventListener("click", async () => {
        const empId = btn.getAttribute("data-id");
        const empName = btn.getAttribute("data-name");
        await generateEmployeeInvite(empId, empName);
      });
    });
  } catch (err) {
    console.error("Failed to load onboarding employees:", err);
  }
}

// Load Admin Audit Logs (hr_admin only)
async function loadAdminAuditLogs() {
  const adminAuditTableBody = document.getElementById("adminAuditTableBody");
  if (!adminAuditTableBody) return;
  try {
    const res = await fetch("/audit/admin");
    if (!res.ok) {
      if (res.status === 403) {
        adminAuditTableBody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:14px;">Access restricted to HR Administrators.</td></tr>`;
      }
      return;
    }
    const data = await res.json();
    const logs = data.logs || [];
    if (logs.length === 0) {
      adminAuditTableBody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:14px;">No administrative audit entries recorded yet.</td></tr>`;
      return;
    }

    adminAuditTableBody.innerHTML = "";
    logs.forEach(log => {
      const tr = document.createElement("tr");
      const dt = log.created_at ? new Date(log.created_at).toLocaleString() : "N/A";
      const detailsStr = log.details && Object.keys(log.details).length ? JSON.stringify(log.details) : "-";
      tr.innerHTML = `
        <td style="white-space:nowrap;font-size:0.75rem;color:var(--text-muted)">${escapeHtml(dt)}</td>
        <td><code>${escapeHtml(log.action)}</code></td>
        <td><strong>${escapeHtml(log.actor_emp_code)}</strong></td>
        <td>${escapeHtml(log.target_emp_code || "-")}</td>
        <td style="font-size:0.75rem;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(detailsStr)}">
          <code>${escapeHtml(detailsStr)}</code>
        </td>
      `;
      adminAuditTableBody.appendChild(tr);
    });
  } catch (err) {
    console.error("Failed to load admin audit logs:", err);
  }
}

// Generate Employee Invite
async function generateEmployeeInvite(employeeId, employeeName) {
  try {
    const res = await fetch(`/employees/${employeeId}/invite`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Failed to generate invite.");

    const fullUrl = `${window.location.origin}${data.activation_link}`;
    if (inviteLinkInput) inviteLinkInput.value = fullUrl;
    if (inviteResultCard) inviteResultCard.classList.remove("hidden");

    showToast(`Invite generated for ${employeeName}!`, "success");
    loadOnboardingEmployees();
  } catch (err) {
    showToast(`Invite error: ${err.message}`, "error");
  }
}

// Copy Invite Link
if (btnCopyInvite) {
  btnCopyInvite.addEventListener("click", () => {
    if (inviteLinkInput && inviteLinkInput.value) {
      navigator.clipboard.writeText(inviteLinkInput.value).then(() => {
        showToast("Activation link copied to clipboard!", "info");
      });
    }
  });
}

// Open Activation Modal directly
if (btnOpenActivate) {
  btnOpenActivate.addEventListener("click", () => {
    if (inviteLinkInput && inviteLinkInput.value) {
      try {
        const url = new URL(inviteLinkInput.value);
        const token = url.searchParams.get("token") || "";
        showActivateModal(token);
      } catch (e) {
        showActivateModal();
      }
    }
  });
}

// Add Employee Form Submission
if (addEmployeeForm) {
  addEmployeeForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = document.getElementById("empNewName").value.trim();
    const work_email = document.getElementById("empNewEmail").value.trim();
    const department = document.getElementById("empNewDept").value;
    const designation = document.getElementById("empNewDesignation").value.trim();
    const join_date = document.getElementById("empNewJoinDate").value;
    const work_location = document.getElementById("empNewLocation").value;

    try {
      const res = await fetch("/employees", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          work_email,
          department,
          designation,
          join_date,
          work_location,
          role: "employee"
        })
      });

      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed to create employee");

      showToast(`Employee ${name} (${data.employee.emp_code}) created! Generating invite...`, "success");
      addEmployeeForm.reset();

      // Automatically generate invite
      await generateEmployeeInvite(data.employee.id, name);
      await loadEmployees();
    } catch (err) {
      showToast(`Creation error: ${err.message}`, "error");
    }
  });
}

if (btnRefreshOnboarding) {
  btnRefreshOnboarding.addEventListener("click", loadOnboardingEmployees);
}

// Initial boot
(async function init() {
  // Check for ?token= in URL for direct activation link visits
  const urlParams = new URLSearchParams(window.location.search);
  const tokenParam = urlParams.get("token");
  if (tokenParam) {
    showActivateModal(tokenParam);
  }

  // Pre-load all employee directory options for manager pickers
  await loadEmployees();

  // Check current authenticated user via safe /auth/session
  try {
    const sessRes = await fetch("/auth/session");
    const sessData = await sessRes.json();
    if (sessData.authenticated && sessData.caller) {
      resetForNewIdentity(sessData.caller);
      hideLoginModal();
      await checkAndRestoreSession(sessData.caller);
    } else {
      if (!tokenParam) {
        resetForNewIdentity(null);
        showLoginModal();
      }
    }
  } catch (err) {
    if (!tokenParam) {
      resetForNewIdentity(null);
      showLoginModal();
    }
  }
})();

if (typeof window !== "undefined") {
  window.STATE = STATE;
  window.resetForNewIdentity = resetForNewIdentity;
  window.loadSession = loadSession;
  window.startNewChat = startNewChat;
  window.appendUserMessage = appendUserMessage;
  window.createAssistantMessage = createAssistantMessage;
  window.checkAndRestoreSession = checkAndRestoreSession;
  window.setHistoryCount = setHistoryCount;
  window.mHistoryCount = mHistoryCount;
  window.mHistorySubtext = mHistorySubtext;
  window.switchTab = switchTab;
  window.openMoreMenu = openMoreMenu;
  window.closeMoreMenu = closeMoreMenu;
  window.toggleMoreMenu = toggleMoreMenu;
  window.renderPolicyChunks = renderPolicyChunks;
  window.renderHrToolResultCard = renderHrToolResultCard;
  window.SUGGEST_NEW_CHAT_AFTER = SUGGEST_NEW_CHAT_AFTER;
  window.SUGGEST_REPEAT_EVERY = SUGGEST_REPEAT_EVERY;
  window.newChatSuggestion = newChatSuggestion;
  window.ncsText = ncsText;
  window.btnNcsStart = btnNcsStart;
  window.btnNcsKeep = btnNcsKeep;
  window.btnNcsClose = btnNcsClose;
  window.getCompletedQueryCount = getCompletedQueryCount;
  window.checkAndShowNewChatSuggestion = checkAndShowNewChatSuggestion;
  window.dismissNewChatSuggestion = dismissNewChatSuggestion;
  window.hideNewChatSuggestion = hideNewChatSuggestion;
  window.getToolStatusMessage = getToolStatusMessage;
  window.getToolResultStatusMessage = getToolResultStatusMessage;
}

