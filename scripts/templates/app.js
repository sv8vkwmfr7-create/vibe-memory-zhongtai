/* 四中台交互层 2.0：状态芯片 · 刷新流水线（两步确认）· 表格增强（搜索/排序/空态）· 返回顶部
   零依赖；file:// 离线打开时自动关闭全部服务器相关功能。 */
(function () {
  "use strict";
  var ONLINE = location.protocol !== "file:";

  /* ================= 状态芯片 + 刷新按钮 ================= */
  function initStatusAndRefresh() {
    var chip = document.getElementById("status-chip");
    var btn = document.getElementById("refresh-btn");
    if (!ONLINE) return; /* 离线：两者保持隐藏 */

    function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;"); }

    function renderChip(d) {
      if (!chip) return;
      var bits = [];
      if (d.head) bits.push("HEAD " + esc(d.head));
      if (d.last_run) bits.push("上次重建 " + esc(String(d.last_run).slice(5, 16)));
      chip.textContent = bits.join(" · ") || "服务在线";
      chip.style.display = "inline-block";
    }

    if (chip) {
      fetch("/api/status").then(function (r) { return r.json(); }).then(renderChip)
        .catch(function () { chip.textContent = "服务在线"; chip.style.display = "inline-block"; });
    }
    if (!btn) return;
    btn.style.display = "inline-block";

    var ARM_MS = 3000, armed = false, armTimer = null, busy = false;
    function disarm() {
      if (armed) { armed = false; btn.classList.remove("armed"); btn.textContent = "⟳ 刷新数据"; }
    }
    function done(btnText) {
      btn.textContent = btnText;
      setTimeout(function () { location.reload(); }, 800);
    }
    function pollUntilIdle(onIdle) {
      fetch("/api/status").then(function (r) { return r.json(); }).then(function (d) {
        if (d.running) setTimeout(function () { pollUntilIdle(onIdle); }, 3000);
        else onIdle(d);
      }).catch(function () { setTimeout(function () { pollUntilIdle(onIdle); }, 5000); });
    }

    btn.addEventListener("click", function () {
      if (busy) return;
      if (!armed) { /* 第一步：两步确认，替代原生 confirm() */
        armed = true; btn.classList.add("armed");
        btn.textContent = "确认运行流水线？（再点一次）";
        armTimer = setTimeout(disarm, ARM_MS);
        return;
      }
      clearTimeout(armTimer); armed = false;
      busy = true; btn.disabled = true; btn.classList.remove("armed");
      btn.innerHTML = '<span class="spin"></span>流水线运行中…';
      fetch("/api/refresh", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d.status === "ok") {
            done("✅ 已完成，刷新页面…");
          } else if (d.status === "busy") {
            btn.textContent = "流水线已在运行，等待完成…";
            pollUntilIdle(function () { done("✅ 已完成，刷新页面…"); });
          } else {
            btn.textContent = "❌ " + String(d.detail || d.status || "失败").slice(-60);
            btn.disabled = false; busy = false;
          }
        })
        .catch(function (e) {
          btn.textContent = "❌ " + e;
          btn.disabled = false; busy = false;
        });
    });
  }

  /* ================= 返回顶部 ================= */
  function initToTop() {
    var b = document.createElement("button");
    b.type = "button"; b.className = "to-top"; b.title = "返回顶部"; b.textContent = "↑";
    document.body.appendChild(b);
    var sync = function () { b.classList.toggle("show", window.scrollY > 500); };
    window.addEventListener("scroll", sync, { passive: true });
    sync();
    b.addEventListener("click", function () { window.scrollTo({ top: 0, behavior: "smooth" }); });
  }

  /* ================= KPI 卡锚点跳转（data-target=面板 id） ================= */
  function initKpiJump() {
    Array.prototype.forEach.call(document.querySelectorAll(".kpi[data-target]"), function (k) {
      k.addEventListener("click", function () {
        var t = document.getElementById(k.getAttribute("data-target"));
        if (!t) return;
        t.scrollIntoView({ behavior: "smooth", block: "start" });
        t.classList.remove("flash");
        void t.offsetWidth; /* 重启动画 */
        t.classList.add("flash");
        setTimeout(function () { t.classList.remove("flash"); }, 1000);
      });
    });
  }

  /* ================= 表格行展开（data-detail=JSON 明细） =================
     点击含 data-detail 的行 → 在其下插入全宽明细行；排序/搜索重排后保持展开状态。 */
  function enhanceExpand(table) {
    var tbody = table.tBodies[0];
    var nCols = table.tHead ? table.tHead.rows[0].cells.length : 0;
    var expanded = [];
    function buildDetail(row) {
      var d;
      try { d = JSON.parse(row.getAttribute("data-detail")); } catch (e) { return null; }
      var td = document.createElement("td");
      td.colSpan = nCols;
      var inner = document.createElement("div");
      inner.className = "row-detail";
      Object.keys(d).forEach(function (k) {
        var kv = document.createElement("div"); kv.className = "kv";
        var lab = document.createElement("span"); lab.textContent = k;
        var val = document.createElement("b"); val.textContent = d[k];
        kv.appendChild(lab); kv.appendChild(val);
        inner.appendChild(kv);
      });
      td.appendChild(inner);
      var tr = document.createElement("tr");
      tr.className = "detail-row";
      tr.appendChild(td);
      return tr;
    }
    function syncDetail(row) {
      var next = row.nextSibling;
      if (next && next.className === "detail-row") next.parentNode.removeChild(next);
      if (expanded.indexOf(row) !== -1) {
        var d = buildDetail(row);
        if (d) row.parentNode.insertBefore(d, row.nextSibling);
        row.classList.add("open");
      } else {
        row.classList.remove("open");
      }
    }
    Array.prototype.forEach.call(tbody.rows, function (row) {
      if (!row.getAttribute("data-detail")) return;
      row.classList.add("has-detail");
      row.addEventListener("click", function (ev) {
        if (ev.target.closest && ev.target.closest("a,button,input")) return;
        var i = expanded.indexOf(row);
        if (i === -1) expanded.push(row); else expanded.splice(i, 1);
        syncDetail(row);
      });
    });
    table._syncExpanded = function () {
      Array.prototype.forEach.call(tbody.rows, function (row) {
        if (expanded.indexOf(row) !== -1) syncDetail(row);
      });
    };
  }

  /* ================= 表格增强：搜索 / 排序 / 分页兜底 =================
     用法：<table class="tbl" data-enhance>。行数少时全量渲染，超过阈值才分页。 */
  var PAGE_SIZE = 60;

  function numVal(t) {
    /* 从单元格文本提取首个数字（支持 1,360 / 87.9% / 12.07ms / 518.8s / 11 个文件） */
    var m = String(t).replace(/,/g, "").match(/-?\d+(\.\d+)?/);
    return m ? parseFloat(m[0]) : null;
  }

  function enhanceTable(table) {
    var thead = table.tHead, tbody = table.tBodies[0];
    if (!thead || !tbody || thead.rows.length === 0) return;
    enhanceExpand(table);
    var headers = Array.prototype.slice.call(thead.rows[0].cells);
    var rows = Array.prototype.slice.call(tbody.rows);
    if (!rows.length || !headers.length) return;

    var state = { q: "", sortCol: -1, sortDir: 1, page: 0 };

    /* 工具条（插在表格所在容器里、表格之前） */
    var tools = document.createElement("div"); tools.className = "tbl-tools";
    var wrapEl = document.createElement("div"); wrapEl.className = "tbl-search-wrap";
    var search = document.createElement("input");
    search.type = "search"; search.className = "tbl-search"; search.placeholder = "搜索本表…";
    var clearBtn = document.createElement("button");
    clearBtn.type = "button"; clearBtn.className = "tbl-clear"; clearBtn.textContent = "✕"; clearBtn.title = "清空";
    wrapEl.appendChild(search); wrapEl.appendChild(clearBtn);
    var count = document.createElement("span"); count.className = "tbl-count";
    tools.appendChild(wrapEl); tools.appendChild(count);
    table.parentNode.insertBefore(tools, table);

    /* 分页条（仅超长表显示） */
    var pager = document.createElement("div"); pager.className = "pgn"; pager.style.display = "none";
    var prev = document.createElement("button"); prev.type = "button"; prev.textContent = "‹ 上一页";
    var next = document.createElement("button"); next.type = "button"; next.textContent = "下一页 ›";
    pager.appendChild(prev); pager.appendChild(next);
    table.parentNode.insertBefore(pager, table.nextSibling);

    function cellText(row, i) { return row.cells[i] ? row.cells[i].textContent.trim() : ""; }

    function render() {
      var q = state.q.toLowerCase();
      var view = rows.filter(function (r) {
        return !q || r.textContent.toLowerCase().indexOf(q) !== -1;
      });
      if (state.sortCol >= 0) {
        var col = state.sortCol, dir = state.sortDir;
        view.sort(function (a, b) {
          var ta = cellText(a, col), tb = cellText(b, col);
          var na = numVal(ta), nb = numVal(tb);
          if (na !== null && nb !== null) return (na - nb) * dir;
          if (na !== null) return 1;          /* 无值行沉底，不随方向翻转 */
          if (nb !== null) return -1;
          return ta.localeCompare(tb, "zh-Hans-CN") * dir;
        });
      }
      var total = view.length;
      var pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      if (state.page >= pages) state.page = pages - 1;
      var start = state.page * PAGE_SIZE;
      var slice = pages > 1 ? view.slice(start, start + PAGE_SIZE) : view;

      tbody.textContent = "";
      if (!total) {
        var tr = document.createElement("tr");
        var td = document.createElement("td");
        td.colSpan = headers.length; td.className = "tbl-empty";
        td.textContent = q ? "没有匹配「" + state.q + "」的行" : "暂无数据";
        tr.appendChild(td); tbody.appendChild(tr);
      } else {
        slice.forEach(function (r) { tbody.appendChild(r); });
      }

      count.textContent = "共 " + total + " 行" + (q ? "（筛选自 " + rows.length + " 行）" : "") +
        (pages > 1 ? " · 第 " + (state.page + 1) + " / " + pages + " 页" : "");
      if (pages > 1) {
        pager.style.display = "flex";
        prev.disabled = state.page === 0;
        next.disabled = state.page >= pages - 1;
      } else {
        pager.style.display = "none";
      }
      if (table._syncExpanded) table._syncExpanded();
    }

    function syncClear() { clearBtn.style.display = search.value ? "block" : "none"; }

    search.addEventListener("input", function () {
      state.q = search.value; state.page = 0; syncClear(); render();
    });
    clearBtn.addEventListener("click", function () {
      search.value = ""; state.q = ""; state.page = 0; syncClear(); render(); search.focus();
    });
    prev.addEventListener("click", function () { if (state.page > 0) { state.page--; render(); } });
    next.addEventListener("click", function () { state.page++; render(); });

    headers.forEach(function (th, i) {
      th.classList.add("th-sort");
      th.addEventListener("click", function () {
        if (state.sortCol === i) state.sortDir *= -1;
        else { state.sortCol = i; state.sortDir = 1; }
        headers.forEach(function (h, j) {
          h.classList.remove("sort-asc", "sort-desc");
          if (j === state.sortCol) h.classList.add(state.sortDir === 1 ? "sort-asc" : "sort-desc");
        });
        state.page = 0; render();
      });
    });

    render();
  }

  function initTables() {
    Array.prototype.forEach.call(document.querySelectorAll("table[data-enhance]"), enhanceTable);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initStatusAndRefresh(); initTables(); initToTop(); initKpiJump(); });
  } else {
    initStatusAndRefresh(); initTables(); initToTop(); initKpiJump();
  }
})();
