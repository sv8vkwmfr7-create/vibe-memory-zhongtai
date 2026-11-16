/* VMCharts — 零依赖 SVG 图表工具（数据中台/评测中台可视化专用，亮色主题） */
(function (global) {
  "use strict";
  var NS = "http://www.w3.org/2000/svg";
  /* 主色阶优先：柱状图缺省统一主蓝（多系列折线才用多色），杜绝随机彩虹。
     多系列折线板为柔和色，避免大红大绿刺眼。 */
  var PALETTE = ["#2563eb", "#7c3aed", "#0ea5e9", "#14b8a6", "#f59e0b", "#ec4899", "#84cc16", "#64748b"];
  var C_LABEL = "#6b7280", C_TICK = "#9ca3af", C_VALUE = "#111827", C_TRACK = "rgba(17,24,39,0.06)", C_MESH = "rgba(17,24,39,0.08)";

  function svgEl(tag, attrs) {
    var el = document.createElementNS(NS, tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }
  /* ---------- 跟随鼠标的浮层提示（替代原生 <title> 的迟钝体验） ---------- */
  var tipEl = null;
  function ensureTip() {
    if (tipEl) return tipEl;
    tipEl = document.createElement("div");
    tipEl.style.cssText = "position:fixed;z-index:99;pointer-events:none;background:rgba(17,24,39,.92);color:#fff;"
      + "font:11.5px/1.5 -apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;padding:5px 9px;"
      + "border-radius:6px;max-width:340px;opacity:0;transition:opacity .12s;white-space:pre-wrap;box-shadow:0 4px 14px rgba(0,0,0,.25);";
    document.body.appendChild(tipEl);
    return tipEl;
  }
  function setTip(el, text) { el.setAttribute("data-tip", text); }
  function bindTip(svg) {
    var tip = ensureTip();
    svg.addEventListener("mousemove", function (e) {
      var t = e.target;
      while (t && t !== svg && !(t.dataset && t.dataset.tip)) t = t.parentNode;
      if (t && t !== svg && t.dataset && t.dataset.tip) {
        tip.textContent = t.dataset.tip;
        var x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 10);
        var y = e.clientY - tip.offsetHeight - 12;
        if (y < 4) y = e.clientY + 16;
        tip.style.left = Math.max(4, x) + "px"; tip.style.top = y + "px"; tip.style.opacity = "1";
      } else {
        tip.style.opacity = "0";
      }
    });
    svg.addEventListener("mouseleave", function () { tip.style.opacity = "0"; });
  }
  function mount(el, w, h) {
    var svg = svgEl("svg", { viewBox: "0 0 " + w + " " + h, preserveAspectRatio: "xMidYMid meet" });
    el.innerHTML = ""; el.appendChild(svg);
    bindTip(svg);
    return svg;
  }
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function fmt(v) {
    if (v == null || isNaN(v)) return "-";
    var a = Math.abs(v);
    if (a >= 1000) return String(Math.round(v));
    if (a >= 100) return String(Math.round(v * 10) / 10);
    if (a >= 1) return String(Math.round(v * 100) / 100);
    return String(Math.round(v * 1000) / 1000);
  }

  /* 水平条形图 data: [{name, value, max?, color?}] */
  function bar(el, data, opts) {
    opts = opts || {};
    var W = opts.width || 640, rowH = opts.rowH || 30, padL = opts.labelW || 170, padR = 76, padT = 6, padB = 6;
    var H = Math.max(80, padT + padB + rowH * data.length);
    var svg = mount(el, W, H);
    var max = opts.max != null ? opts.max : Math.max.apply(null, data.map(function (d) { return d.max || d.value; }).concat([1]));
    data.forEach(function (d, i) {
      var y = padT + i * rowH;
      var t = svgEl("text", { x: padL - 10, y: y + rowH / 2 + 4, "text-anchor": "end", "font-size": 11.5, fill: C_LABEL });
      t.textContent = d.name; svg.appendChild(t);
      var track = svgEl("rect", { x: padL, y: y + 6, width: W - padL - padR, height: rowH - 12, rx: 4, fill: C_TRACK });
      svg.appendChild(track);
      var bw = Math.max(2, (W - padL - padR) * (d.value / max));
      var c = d.color || PALETTE[0];
      var r = svgEl("rect", { x: padL, y: y + 6, width: bw, height: rowH - 12, rx: 4, fill: c, opacity: 0.92 });
      setTip(r, d.name + " : " + (opts.fmt || fmt)(d.value));
      svg.appendChild(r);
      var v = svgEl("text", { x: padL + bw + 8, y: y + rowH / 2 + 4, "font-size": 11.5, fill: C_VALUE, "font-family": "Consolas,monospace" });
      v.textContent = (opts.fmt || fmt)(d.value); svg.appendChild(v);
    });
  }

  /* 折线/面积图 series: [{name, values, color?}], labels: []
     多系列自动关闭面积填充；数值标注仅在 opts.showPoints 时画（单系列不重叠才用）；
     图例可点击隐藏/显示对应系列。 */
  function line(el, labels, series, opts) {
    opts = opts || {};
    var W = opts.width || 640, H = opts.height || 240;
    var padL = 44, padR = 14, padT = 14, padB = 30;
    var svg = mount(el, W, H);
    var n = labels.length;
    var allVals = series.reduce(function (a, s) { return a.concat(s.values); }, []);
    var vmax = opts.max != null ? opts.max : Math.max.apply(null, allVals.concat([1])) * 1.12;
    var vmin = opts.min != null ? opts.min : 0;
    function X(i) { return padL + (n <= 1 ? (W - padL - padR) / 2 : (W - padL - padR) * i / (n - 1)); }
    function Y(v) { return padT + (H - padT - padB) * (1 - (v - vmin) / (vmax - vmin || 1)); }
    for (var g = 0; g <= 4; g++) {
      var gv = vmin + (vmax - vmin) * g / 4, gy = Y(gv);
      svg.appendChild(svgEl("line", { x1: padL, y1: gy, x2: W - padR, y2: gy, class: "gridline" }));
      var t = svgEl("text", { x: padL - 7, y: gy + 3.5, "text-anchor": "end", "font-size": 10, fill: C_TICK });
      t.textContent = (opts.fmt || fmt)(gv); svg.appendChild(t);
    }
    labels.forEach(function (lb, i) {
      if (n > 12 && i % Math.ceil(n / 10) !== 0) return;
      var t = svgEl("text", { x: X(i), y: H - 10, "text-anchor": "middle", "font-size": 10, fill: C_TICK });
      t.textContent = lb; svg.appendChild(t);
    });
    var showArea = series.length === 1 && !opts.noArea;
    series.forEach(function (s, si) {
      var c = s.color || PALETTE[si % PALETTE.length];
      var pts = s.values.map(function (v, i) { return X(i) + "," + Y(v); }).join(" ");
      function mark(elx) { elx.setAttribute("data-series", si); return elx; }
      if (showArea) {
        svg.appendChild(mark(svgEl("polygon", { points: padL + "," + Y(vmin) + " " + pts + " " + X(n - 1) + "," + Y(vmin), fill: c, opacity: 0.08 })));
      }
      svg.appendChild(mark(svgEl("polyline", { points: pts, fill: "none", stroke: c, "stroke-width": 2, "stroke-linejoin": "round" })));
      s.values.forEach(function (v, i) {
        var dot = mark(svgEl("circle", { cx: X(i), cy: Y(v), r: 3, fill: "#ffffff", stroke: c, "stroke-width": 2 }));
        setTip(dot, s.name + " · " + labels[i] + " : " + (opts.fmt || fmt)(v));
        svg.appendChild(dot);
        if (opts.showPoints && n <= 14) {
          var t = mark(svgEl("text", { x: X(i), y: Math.max(padT + 9, Y(v) - 8), "text-anchor": "middle", "font-size": 9.5, fill: c, "font-family": "Consolas,monospace" }));
          t.textContent = (opts.fmt || fmt)(v); svg.appendChild(t);
        }
      });
    });
    if (series.length > 1 || opts.legend) {
      var lg = document.createElement("div"); lg.className = "legend";
      series.forEach(function (s, si) {
        var c = s.color || PALETTE[si % PALETTE.length];
        lg.innerHTML += '<span data-si="' + si + '" style="cursor:pointer"><span class="sw" style="background:' + c + '"></span>' + esc(s.name) + "</span>";
      });
      el.appendChild(lg);
      Array.prototype.forEach.call(lg.children, function (sp) {
        sp.addEventListener("click", function () {
          var si = sp.getAttribute("data-si");
          var on = sp.style.opacity !== "0.35";
          sp.style.opacity = on ? "0.35" : "";
          sp.style.textDecoration = on ? "line-through" : "";
          Array.prototype.forEach.call(svg.querySelectorAll('[data-series="' + si + '"]'), function (elx) {
            elx.style.display = on ? "none" : "";
          });
        });
      });
    }
  }

  /* 雷达图 data: [{name, value(0-1)}] */
  function radar(el, data, opts) {
    opts = opts || {};
    var W = opts.width || 420, H = W * 0.86, cx = W / 2, cy = H / 2 + 6, R = Math.min(W, H) / 2 - 44;
    var svg = mount(el, W, H);
    var n = data.length;
    function pt(i, r) {
      var a = -Math.PI / 2 + 2 * Math.PI * i / n;
      return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
    }
    for (var ring = 1; ring <= 4; ring++) {
      var pts = [];
      for (var i = 0; i < n; i++) pts.push(pt(i, R * ring / 4).join(","));
      svg.appendChild(svgEl("polygon", { points: pts.join(" "), fill: "none", stroke: C_MESH, "stroke-width": 1 }));
    }
    for (var i = 0; i < n; i++) {
      var p = pt(i, R);
      svg.appendChild(svgEl("line", { x1: cx, y1: cy, x2: p[0], y2: p[1], stroke: C_MESH }));
      var lp = pt(i, R + 26);
      var t = svgEl("text", { x: lp[0], y: lp[1] + 4, "text-anchor": "middle", "font-size": 11, fill: C_LABEL });
      t.textContent = data[i].name; svg.appendChild(t);
      var vp = pt(i, R + 14);
      var vt = svgEl("text", { x: vp[0], y: vp[1] + 3, "text-anchor": "middle", "font-size": 9, fill: "#2563eb", "font-family": "Consolas,monospace", opacity: 0.9 });
      vt.textContent = fmt(data[i].value); svg.appendChild(vt);
    }
    var poly = [];
    data.forEach(function (d, i) { poly.push(pt(i, R * Math.max(0.02, Math.min(1, d.value))).join(",")); });
    svg.appendChild(svgEl("polygon", { points: poly.join(" "), fill: "rgba(37,99,235,0.14)", stroke: "#2563eb", "stroke-width": 2 }));
    data.forEach(function (d, i) {
      var p = pt(i, R * Math.max(0.02, Math.min(1, d.value)));
      svg.appendChild(svgEl("circle", { cx: p[0], cy: p[1], r: 3.5, fill: "#2563eb" }));
    });
  }

  /* 环形进度 value 0-1 */
  function donut(el, value, opts) {
    opts = opts || {};
    var W = opts.width || 180, H = W, cx = W / 2, cy = H / 2, R = W / 2 - 12, r = R - 13;
    var svg = mount(el, W, H);
    var v = Math.max(0, Math.min(1, value));
    var a0 = -Math.PI / 2, a1 = a0 + 2 * Math.PI * v;
    var large = v > 0.5 ? 1 : 0;
    var color = opts.color || (v >= 0.85 ? "#059669" : v >= 0.6 ? "#2563eb" : v >= 0.4 ? "#d97706" : "#dc2626");
    function P(a, rad) { return [cx + rad * Math.cos(a), cy + rad * Math.sin(a)]; }
    var p0 = P(a0, R), p1 = P(a1, R), q1 = P(a1, r), q0 = P(a0, r);
    var d = "M " + p0.join(" ") + " A " + R + " " + R + " 0 " + large + " 1 " + p1.join(" ") +
            " L " + q1.join(" ") + " A " + r + " " + r + " 0 " + large + " 0 " + q0.join(" ") + " Z";
    if (v > 0.001) svg.appendChild(svgEl("path", { d: d, fill: color, opacity: 0.9 }));
    var t = svgEl("text", { x: cx, y: cy + 2, "text-anchor": "middle", "font-size": 26, "font-weight": 700, fill: C_VALUE, "font-family": "Consolas,monospace" });
    t.textContent = Math.round(v * 100) + "%"; svg.appendChild(t);
    var t2 = svgEl("text", { x: cx, y: cy + 24, "text-anchor": "middle", "font-size": 11, fill: C_LABEL });
    t2.textContent = opts.label || ""; svg.appendChild(t2);
  }

  /* 分组小柱状图（榜单/分类型得分）data: [{name, value}] value 0-1 */
  function vbar(el, data, opts) {
    opts = opts || {};
    var W = opts.width || 640, H = opts.height || 230;
    var padL = 36, padR = 10, padT = 18, padB = 42;
    var svg = mount(el, W, H);
    var n = data.length, bw = (W - padL - padR) / n;
    var fmtFn = opts.fmt || (opts.percent === false ? fmt : function (v) { return Math.round(v * 100) + "%"; });
    var vmax = opts.max || 1;
    function Y(v) { return padT + (H - padT - padB) * (1 - v / vmax); }
    [0, 0.25, 0.5, 0.75, 1].forEach(function (g) {
      var gv = g * vmax;
      svg.appendChild(svgEl("line", { x1: padL, y1: Y(gv), x2: W - padR, y2: Y(gv), class: "gridline" }));
      var t = svgEl("text", { x: padL - 6, y: Y(gv) + 3, "text-anchor": "end", "font-size": 9.5, fill: C_TICK });
      t.textContent = fmtFn(gv); svg.appendChild(t);
    });
    data.forEach(function (d, i) {
      var x = padL + bw * i + bw * 0.18, w = bw * 0.64;
      var c = d.color || opts.color || PALETTE[0];
      var rect = svgEl("rect", { x: x, y: Y(Math.max(0.01, d.value)), width: w, height: H - padB - Y(d.value), rx: 4, fill: c, opacity: 0.9 });
      setTip(rect, d.name + " : " + fmtFn(d.value));
      svg.appendChild(rect);
      var vt = svgEl("text", { x: x + w / 2, y: Y(d.value) - 6, "text-anchor": "middle", "font-size": 10.5, fill: C_VALUE, "font-family": "Consolas,monospace" });
      vt.textContent = fmtFn(d.value); svg.appendChild(vt);
      var lt = svgEl("text", { x: x + w / 2, y: H - padB + 16, "text-anchor": "middle", "font-size": 10.5, fill: C_LABEL });
      lt.textContent = d.name; svg.appendChild(lt);
    });
  }

  /* 迷你趋势 sparkline：行式趋势卡用。无轴无框，固定 y 域保证行间可比，悬浮逐点提示 */
  function spark(el, values, opts) {
    opts = opts || {};
    var W = opts.width || 150, H = opts.height || 40, pad = 5;
    var svg = mount(el, W, H);
    var n = values.length;
    var vmax = opts.max != null ? opts.max : Math.max.apply(null, values.concat([1]));
    var vmin = opts.min != null ? opts.min : 0;
    function X(i) { return pad + (n <= 1 ? (W - 2 * pad) / 2 : (W - 2 * pad) * i / (n - 1)); }
    function Y(v) { return pad + (H - 2 * pad) * (1 - (v - vmin) / (vmax - vmin || 1)); }
    var c = opts.color || PALETTE[0];
    var pts = values.map(function (v, i) { return X(i) + "," + Y(v); }).join(" ");
    svg.appendChild(svgEl("polyline", { points: pts, fill: "none", stroke: c, "stroke-width": 1.8,
      "stroke-linejoin": "round", "stroke-linecap": "round", opacity: 0.85 }));
    values.forEach(function (v, i) {
      var dot = svgEl("circle", { cx: X(i), cy: Y(v), r: i === n - 1 ? 3 : 2,
        fill: i === n - 1 ? c : "#ffffff", stroke: c, "stroke-width": 1.5 });
      setTip(dot, (opts.labels ? opts.labels[i] : String(i + 1)) + " : " + (opts.fmt || fmt)(v));
      svg.appendChild(dot);
    });
  }

  /* 简单热力格 heatmap: rows[{name, cells:[{v, label?}]}] */
  function heatmap(el, rows, opts) {
    opts = opts || {};
    var W = opts.width || 640, cell = opts.cell || 30, padL = opts.labelW || 130, padT = 6, gap = 4;
    var H = padT + rows.length * (cell + gap) + 6;
    var svg = mount(el, W, H);
    rows.forEach(function (r, ri) {
      var y = padT + ri * (cell + gap);
      var t = svgEl("text", { x: padL - 10, y: y + cell / 2 + 4, "text-anchor": "end", "font-size": 11, fill: C_LABEL });
      t.textContent = r.name; svg.appendChild(t);
      r.cells.forEach(function (c, ci) {
        var x = padL + ci * (cell + gap);
        var v = Math.max(0, Math.min(1, c.v));
        var col = v >= 0.85 ? "#059669" : v >= 0.6 ? "#2563eb" : v >= 0.4 ? "#d97706" : v > 0 ? "#dc2626" : C_TRACK;
        var rect = svgEl("rect", { x: x, y: y, width: cell, height: cell, rx: 5, fill: col, opacity: v > 0 ? 0.88 : 1 });
        setTip(rect, r.name + " · " + (c.label || ci) + " : " + fmt(c.v));
        svg.appendChild(rect);
        var vt = svgEl("text", { x: x + cell / 2, y: y + cell / 2 + 3.5, "text-anchor": "middle", "font-size": 9, fill: "#ffffff", "font-family": "Consolas,monospace", "font-weight": 700 });
        vt.textContent = c.text != null ? c.text : ""; svg.appendChild(vt);
      });
    });
  }

  global.VMCharts = { bar: bar, line: line, radar: radar, donut: donut, vbar: vbar, heatmap: heatmap, spark: spark, PALETTE: PALETTE, fmt: fmt, esc: esc };
})(window);
