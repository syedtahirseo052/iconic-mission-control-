/* Mission Control v2 — assign-task modal, add-agent modal, talk-to-agent
   slide-over, toasts. Vanilla JS, no dependencies. */
(function () {
  "use strict";

  function $(sel, root) { return (root || document).querySelector(sel); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function showToast(msg) {
    var t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () { t.hidden = true; }, 3200);
  }

  async function api(path, opts) {
    var res = await fetch(path, opts);
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("Session expired — please log in again.");
    }
    var data = {};
    try { data = await res.json(); } catch (e) { /* keep {} */ }
    if (!res.ok || data.ok === false) {
      throw new Error((data && data.error) || "Request failed (" + res.status + ").");
    }
    return data;
  }

  /* ---------------- assign-task modal ---------------- */
  var assignModal = $("#assign-modal"),
      assignForm = $("#assign-form"),
      assignAgent = $("#assign-agent"),
      assignError = $("#assign-error");

  async function loadAgentOptions(select, preselect) {
    var data = await api("/api/meta");
    select.innerHTML = "";
    data.agents.forEach(function (name) {
      var opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (preselect && name === preselect) opt.selected = true;
      select.appendChild(opt);
    });
  }

  async function openAssign(agentName) {
    assignError.hidden = true;
    assignForm.reset();
    $("#assign-priority").value = "MEDIUM";
    try {
      await loadAgentOptions(assignAgent, agentName || null);
    } catch (err) {
      assignError.textContent = err.message;
      assignError.hidden = false;
      return;
    }
    assignModal.hidden = false;
    document.body.classList.add("no-scroll");
    $("#assign-objective").focus();
  }

  function closeModals() {
    assignModal.hidden = true;
    var m2 = $("#addagent-modal");
    if (m2) m2.hidden = true;
    document.body.classList.remove("no-scroll");
  }

  document.addEventListener("click", function (e) {
    var ab = e.target.closest("[data-assign]");
    if (ab) { openAssign(ab.getAttribute("data-assign")); return; }
    if (e.target.closest("[data-close-modal]")) { closeModals(); return; }
    if (e.target === assignModal || e.target === $("#addagent-modal")) {
      closeModals(); return;
    }
    if (e.target === $("#thread-overlay")) { closeThread(); return; }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeModals(); closeThread(); }
  });

  var assignTaskBtn = $("#assign-task-btn");
  if (assignTaskBtn) {
    assignTaskBtn.addEventListener("click", function () { openAssign(null); });
  }

  assignForm.addEventListener("submit", function (e) {
    e.preventDefault();
    assignError.hidden = true;
    var payload = {
      agent: assignAgent.value,
      objective: $("#assign-objective").value.trim(),
      priority: $("#assign-priority").value,
      deadline: $("#assign-deadline").value,
      input_text: $("#assign-input").value.trim()
    };
    if (!payload.objective) {
      assignError.textContent = "Objective is required.";
      assignError.hidden = false;
      return;
    }
    api("/api/assign-task", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (data) {
      closeModals();
      showToast("Task " + data.task_id + " assigned.");
      setTimeout(function () { window.location.reload(); }, 900);
    }).catch(function (err) {
      assignError.textContent = err.message;
      assignError.hidden = false;
    });
  });

  /* ---------------- add-agent modal ---------------- */
  var aaModal = $("#addagent-modal"),
      aaForm = $("#addagent-form"),
      aaError = $("#addagent-error");

  var addAgentBtn = $("#add-agent-btn");
  if (addAgentBtn) {
    addAgentBtn.addEventListener("click", function () {
      aaError.hidden = true;
      aaForm.reset();
      $("#aa-status").value = "STANDBY";
      api("/api/meta").then(function (data) {
        var dept = $("#aa-dept"), rep = $("#aa-reports");
        dept.innerHTML = ""; rep.innerHTML = "";
        data.departments.forEach(function (d) {
          var o = document.createElement("option");
          o.value = d; o.textContent = d; dept.appendChild(o);
        });
        var ceo = document.createElement("option");
        ceo.value = "Iconic AI CEO"; ceo.textContent = "Iconic AI CEO";
        rep.appendChild(ceo);
        data.agents.forEach(function (n) {
          var o = document.createElement("option");
          o.value = n; o.textContent = n; rep.appendChild(o);
        });
        aaModal.hidden = false;
        document.body.classList.add("no-scroll");
        $("#aa-name").focus();
      }).catch(function (err) {
        showToast("Could not load form data: " + err.message);
      });
    });
  }

  aaForm.addEventListener("submit", function (e) {
    e.preventDefault();
    aaError.hidden = true;
    var payload = {
      name: $("#aa-name").value.trim(),
      department: $("#aa-dept").value,
      reports_to: $("#aa-reports").value,
      role_summary: $("#aa-role").value.trim(),
      kpis: $("#aa-kpis").value.trim(),
      status: $("#aa-status").value
    };
    if (!payload.name || !payload.role_summary) {
      aaError.textContent = "Agent name and role summary are required.";
      aaError.hidden = false;
      return;
    }
    api("/api/add-agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (data) {
      closeModals();
      showToast("Agent '" + data.name + "' added.");
      setTimeout(function () { window.location.reload(); }, 900);
    }).catch(function (err) {
      aaError.textContent = err.message;
      aaError.hidden = false;
    });
  });

  /* ---------------- talk-to-agent slide-over ---------------- */
  var threadPanel = $("#thread-panel"),
      threadOverlay = $("#thread-overlay"),
      threadBox = $("#thread-messages"),
      threadForm = $("#thread-form"),
      threadInput = $("#thread-input"),
      threadError = $("#thread-error"),
      threadAgent = null,
      threadTimer = null;

  function renderThread(messages) {
    threadBox.innerHTML = "";
    if (!messages.length) {
      var p = document.createElement("p");
      p.className = "empty-note";
      p.textContent = "No messages yet. Write the first one below — it will be delivered to " +
        threadAgent + " as a task.";
      threadBox.appendChild(p);
      return;
    }
    messages.forEach(function (m) {
      var wrap = document.createElement("div");
      wrap.className = "msg";
      var bubble = document.createElement("div");
      bubble.className = "msg-out";
      var txt = document.createElement("p");
      txt.textContent = m.text;
      bubble.appendChild(txt);
      var meta = document.createElement("div");
      meta.className = "msg-meta";
      meta.textContent = (m.task_id || "") + " · " + (m.created_at || "") +
        " · " + (m.status || "");
      bubble.appendChild(meta);
      wrap.appendChild(bubble);
      if (m.output) {
        var rep = document.createElement("div");
        rep.className = "msg-in";
        var lab = document.createElement("div");
        lab.className = "msg-meta";
        lab.textContent = "Agent output (recorded in sheet):";
        var pre = document.createElement("pre");
        pre.textContent = m.output;
        rep.appendChild(lab);
        rep.appendChild(pre);
        wrap.appendChild(rep);
      } else if (m.status === "OPEN" || m.status === "IN_PROGRESS") {
        var wait = document.createElement("div");
        wait.className = "msg-wait";
        wait.textContent = "Waiting for the agent to record output.";
        wrap.appendChild(wait);
      }
      threadBox.appendChild(wrap);
    });
    threadBox.scrollTop = threadBox.scrollHeight;
  }

  function loadThread() {
    if (!threadAgent) return;
    api("/api/thread?agent=" + encodeURIComponent(threadAgent))
      .then(function (data) { renderThread(data.messages); })
      .catch(function () { /* keep existing content on transient errors */ });
  }

  function openThread(agent) {
    threadAgent = agent;
    threadError.hidden = true;
    $("#thread-title").textContent = agent;
    threadPanel.hidden = false;
    threadOverlay.hidden = false;
    document.body.classList.add("no-scroll");
    threadBox.innerHTML = '<p class="empty-note">Loading…</p>';
    loadThread();
    if (threadTimer) clearInterval(threadTimer);
    threadTimer = setInterval(loadThread, 30000);
    threadInput.focus();
  }

  function closeThread() {
    threadPanel.hidden = true;
    threadOverlay.hidden = true;
    document.body.classList.remove("no-scroll");
    threadAgent = null;
    if (threadTimer) { clearInterval(threadTimer); threadTimer = null; }
  }

  document.addEventListener("click", function (e) {
    var mb = e.target.closest("[data-message]");
    if (mb) { openThread(mb.getAttribute("data-message")); return; }
  });
  $("#thread-close").addEventListener("click", closeThread);

  threadForm.addEventListener("submit", function (e) {
    e.preventDefault();
    threadError.hidden = true;
    var text = threadInput.value.trim();
    if (!text || !threadAgent) return;
    api("/api/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent: threadAgent, text: text })
    }).then(function () {
      threadInput.value = "";
      loadThread();
    }).catch(function (err) {
      threadError.textContent = err.message;
      threadError.hidden = false;
    });
  });

  /* ---------------- topbar search (filters page items) ---------------- */
  var topSearch = $("#top-search");
  if (topSearch) {
    topSearch.addEventListener("input", function () {
      var q = topSearch.value.trim().toLowerCase();
      document.querySelectorAll("[data-search]").forEach(function (el) {
        el.style.display =
          el.getAttribute("data-search").toLowerCase().indexOf(q) !== -1 ? "" : "none";
      });
    });
    document.addEventListener("keydown", function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        topSearch.focus();
      }
    });
  }

  /* ---------------- target editor modal ---------------- */
  var targetModal = $("#target-modal"),
      targetForm = $("#target-form"),
      targetError = $("#target-error"),
      targetMonth = $("#target-month"),
      targetAmount = $("#target-amount");

  var targetEditBtn = $("#target-edit-btn");
  if (targetEditBtn) {
    targetEditBtn.addEventListener("click", function () {
      targetError.hidden = true;
      targetForm.reset();
      api("/api/targets").then(function (data) {
        var now = new Date();
        var ym = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0");
        targetMonth.value = ym;
        if (data.targets && data.targets[ym] != null) {
          targetAmount.value = data.targets[ym];
        }
        targetModal.hidden = false;
        document.body.classList.add("no-scroll");
        targetAmount.focus();
      }).catch(function (err) {
        showToast("Could not load targets: " + err.message);
      });
    });
  }
  if (targetForm) {
    targetForm.addEventListener("submit", function (e) {
      e.preventDefault();
      targetError.hidden = true;
      var payload = {
        month: targetMonth.value,
        amount: parseFloat(targetAmount.value)
      };
      if (!payload.month || isNaN(payload.amount)) {
        targetError.textContent = "Month and amount are required.";
        targetError.hidden = false;
        return;
      }
      api("/api/targets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).then(function () {
        targetModal.hidden = true;
        document.body.classList.remove("no-scroll");
        showToast("Target saved.");
        setTimeout(function () { window.location.reload(); }, 900);
      }).catch(function (err) {
        targetError.textContent = err.message;
        targetError.hidden = false;
      });
    });
  }

  /* close target modal with the others */
  document.addEventListener("click", function (e) {
    if (targetModal && e.target === targetModal) {
      targetModal.hidden = true;
      document.body.classList.remove("no-scroll");
    }
    var tm = e.target.closest("[data-close-modal]");
    if (tm && targetModal) {
      targetModal.hidden = true;
      document.body.classList.remove("no-scroll");
    }
  });
})();
