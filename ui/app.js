/* dq 分析端 UI — 全部数据来自 /api，无任何前端编造 */
"use strict";
const $ = (s) => document.querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};
const fmt = (n) => (n === null || n === undefined) ? "—" : Number(n).toLocaleString();
const ts = (t) => t ? new Date(t * 1000).toLocaleTimeString() : "—";

let STATE = null, TAB = "dash", recOffset = 0, selectedCombo = null;

async function api(path, body) {
  const opt = body === undefined ? {} :
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

/* ---------- tooltip（值为主、标签为辅；textContent 防注入） ---------- */
const tip = $("#tooltip");
function showTip(e, value, label) {
  tip.replaceChildren();
  tip.appendChild(el("div", "tv", value));
  tip.appendChild(el("div", null, label));
  tip.style.display = "block";
  tip.style.left = Math.min(e.clientX + 14, innerWidth - 220) + "px";
  tip.style.top = (e.clientY + 14) + "px";
}
function hideTip() { tip.style.display = "none"; }
function bindTip(node, value, label) {
  node.addEventListener("pointermove", (e) => showTip(e, value, label));
  node.addEventListener("pointerleave", hideTip);
  node.addEventListener("focus", (e) => showTip(e, value, label));
  node.addEventListener("blur", hideTip);
  node.tabIndex = 0;
}

/* ---------- 通用 ---------- */
const ruleIdx = (rid) => STATE.rules.findIndex((r) => r.rule_id === rid);
const ruleName = (rid) => (STATE.rules.find((r) => r.rule_id === rid) || {}).name || rid;
function chip(rid) {
  const c = el("span", "chip c" + (ruleIdx(rid) % 8), ruleName(rid));
  return c;
}
function stateBadge(st) {
  const map = { reported: ["ok", "已生效"], pending: ["warn", "待重算"],
                retracted: ["dim", "已撤回"], stale: ["err", "已拒绝"] };
  const [cls, txt] = map[st] || ["dim", st];
  return el("span", "badge " + cls, txt);
}
function barRow(name, value, max, opts = {}) {
  const row = el("div", "bar-row");
  row.appendChild(el("div", "name", name));
  const track = el("div", "bar-track");
  const bar = el("div", "bar" + (opts.total ? " total" : ""));
  bar.style.width = Math.max(0.5, (value / (max || 1)) * 100) + "%";
  bindTip(bar, fmt(value), opts.tip || name);
  track.appendChild(bar);
  row.appendChild(track);
  row.appendChild(el("div", "val", fmt(value)));
  return row;
}

/* ---------- 总览 ---------- */
function renderDash(root) {
  const o = STATE.live.overall, comp = STATE.live.compression;
  const heroRow = el("div", "hero-row");
  const hero = el("div", "card hero");
  hero.appendChild(el("div", "lbl", "去重后受影响记录（精确，精确集合求并集）"));
  hero.appendChild(el("div", "num", fmt(o.union_exact)));
  const sub = el("div", "sub");
  sub.appendChild(el("span", "badge exact", "精确"));
  sub.appendChild(document.createTextNode(` 输入 v${STATE.live.input_version} · 共 ${fmt(STATE.record_count)} 条记录`));
  hero.appendChild(sub);
  heroRow.appendChild(hero);

  const t1 = el("div", "tile");
  t1.appendChild(el("div", "l", "各规则命中之和（无效总数）"));
  const v1 = el("div", "v strike", fmt(o.naive_sum_invalid_total));
  t1.appendChild(v1);
  t1.appendChild(el("div", "sub", "⚠ 同一记录可命中多规则，各项相加 ≠ 总体"));
  heroRow.appendChild(t1);

  const t2 = el("div", "tile");
  t2.appendChild(el("div", "l", "被重复计数"));
  t2.appendChild(el("div", "v", fmt(o.double_counted)));
  t2.appendChild(el("div", "sub", "朴素求和比去重总体多出的计数"));
  heroRow.appendChild(t2);

  const t3 = el("div", "tile");
  t3.appendChild(el("div", "l", "HLL 基数估计"));
  const v3 = el("div", "v", "≈ " + fmt(o.union_hll_estimate));
  t3.appendChild(v3);
  const s3 = el("div", "sub");
  s3.appendChild(el("span", "badge approx", "近似"));
  s3.appendChild(document.createTextNode(
    ` 相对误差 ±${(o.hll_relative_error * 100).toFixed(2)}%，不可当作精确值` +
    (o.hll_rebuilt_after_deletion ? "；删除后已重建" : "")));
  t3.appendChild(s3);
  heroRow.appendChild(t3);

  const t4 = el("div", "tile");
  t4.appendChild(el("div", "l", "精确压缩（Roaring）"));
  t4.appendChild(el("div", "v", comp.ratio === null ? "—" : (comp.ratio * 100).toFixed(1) + "%"));
  t4.appendChild(el("div", "sub",
    `${fmt(comp.roaring_exact_bytes)}B vs 原始 ${fmt(comp.raw_id_list_bytes)}B · 支持交集追溯与合法删除`));
  heroRow.appendChild(t4);
  root.appendChild(heroRow);

  // 各规则命中（精确）+ 去重总体参考
  const card = el("div", "card");
  card.appendChild(el("h2", null, "各规则命中 vs 去重总体（同一坐标轴，精确值）"));
  const rules = STATE.rules.filter((r) => r.run_state === "reported");
  const pending = STATE.rules.filter((r) => r.run_state !== "reported");
  const max = Math.max(o.union_exact, ...rules.map((r) => r.exact_count || 0), 1);
  card.appendChild(barRow("去重后总体（精确）", o.union_exact, max,
    { total: true, tip: "精确并集：同一记录只计一次" }));
  card.appendChild(el("div", "divider"));
  for (const r of [...rules].sort((a, b) => b.exact_count - a.exact_count))
    card.appendChild(barRow(r.name, r.exact_count, max, { tip: `规则 ${r.rule_id} v${r.version}` }));
  for (const r of pending) {
    const row = el("div", "bar-row");
    row.appendChild(el("div", "name", r.name));
    const note = el("div", "muted small", "已撤回，等待执行器按 v" + r.version + " 重算");
    row.appendChild(note);
    card.appendChild(row);
  }
  root.appendChild(card);

  // 两两交集热图（顺序单色调）
  const pw = STATE.live.pairwise;
  if (pw.length) {
    const hc = el("div", "card");
    hc.appendChild(el("h2", null, "规则两两交集（精确，可追溯到记录）"));
    const ids = STATE.rules.map((r) => r.rule_id);
    const lookup = {};
    for (const p of pw) lookup[p.a + "|" + p.b] = p.intersection_exact;
    const maxI = Math.max(...pw.map((p) => p.intersection_exact), 1);
    const table = el("table", "heat");
    const head = el("tr");
    head.appendChild(el("th"));
    for (const rid of ids) head.appendChild(el("th", null, ruleName(rid)));
    table.appendChild(head);
    for (const a of ids) {
      const tr = el("tr");
      tr.appendChild(el("th", null, ruleName(a)));
      for (const b of ids) {
        const td = el("td", "cell");
        if (a === b) {
          const own = (STATE.rules.find((r) => r.rule_id === a) || {}).exact_count;
          td.textContent = fmt(own);
        } else {
          const v = lookup[a + "|" + b] ?? lookup[b + "|" + a] ?? 0;
          td.textContent = v ? fmt(v) : "";
          if (v) {
            td.style.background = `color-mix(in srgb, var(--s1) ${Math.round(8 + 62 * v / maxI)}%, transparent)`;
            bindTip(td, fmt(v), `${ruleName(a)} ∩ ${ruleName(b)}（精确交集）`);
            td.classList.add("clickable");
            td.addEventListener("click", () => { switchTab("overlap"); selectCombo([a, b]); });
          }
        }
        tr.appendChild(td);
      }
      table.appendChild(tr);
    }
    hc.appendChild(table);
    hc.appendChild(el("div", "muted small", "点击单元格可跳到重叠组合并追溯原始记录"));
    root.appendChild(hc);
  }

  // 发布状态
  const pc = el("div", "card");
  pc.appendChild(el("h2", null, "发布状态"));
  const row = el("div", "row");
  if (STATE.published) {
    const p = STATE.published;
    const lag = p.payload.overall.union_exact !== o.union_exact ||
                p.payload.input_version !== STATE.live.input_version;
    row.appendChild(el("span", null, `已发布快照 ${p.snapshot_id}（${ts(p.published_at)}）`));
    row.appendChild(lag ? el("span", "badge warn", "发布滞后于当前计算")
                        : el("span", "badge ok", "与当前计算一致"));
    row.appendChild(el("span", "muted small",
      `快照中去重总体=${fmt(p.payload.overall.union_exact)}，当前=${fmt(o.union_exact)}`));
  } else {
    row.appendChild(el("span", "muted", "尚未发布任何快照 — 到「流水线与模拟」执行发布"));
  }
  pc.appendChild(row);
  root.appendChild(pc);
}

/* ---------- 规则 ---------- */
const KIND_PARAMS = {
  missing: [["field", "字段"]],
  regex: [["field", "字段"], ["pattern", "正则"]],
  range: [["field", "字段"], ["min", "最小值"], ["max", "最大值"]],
  in_list: [["field", "字段"], ["values", "合法值(逗号分隔)"], ["values_from", "或命名列表(countries/plans)"]],
  duplicate: [["field", "字段"]],
  date_not_future: [["field", "字段"]],
};
function ruleForm(target, initial, onSave) {
  target.replaceChildren();
  const nameIn = el("input"); nameIn.value = initial.name || ""; nameIn.placeholder = "规则名称";
  const kindSel = el("select");
  for (const k of Object.keys(KIND_PARAMS)) kindSel.appendChild(el("option", null, k)).value = k;
  kindSel.value = initial.kind || "missing";
  const paramsBox = el("span");
  const inputs = {};
  function buildParams() {
    paramsBox.replaceChildren();
    for (const [key, label] of KIND_PARAMS[kindSel.value]) {
      const lab = el("label", null, label);
      const inp = el("input");
      inp.placeholder = label;
      const pv = (initial.params || {})[key];
      inp.value = Array.isArray(pv) ? pv.join(",") : (pv ?? "");
      inputs[key] = inp;
      lab.appendChild(inp);
      paramsBox.appendChild(lab);
    }
  }
  kindSel.addEventListener("change", buildParams);
  buildParams();
  const save = el("button", "primary", "保存（产生新版本）");
  save.addEventListener("click", async () => {
    const params = {};
    for (const [key, inp] of Object.entries(inputs)) {
      if (!inp.value.trim()) continue;
      if (key === "values") params.values = inp.value.split(",").map((s) => s.trim()).filter(Boolean);
      else if (key === "min" || key === "max") params[key] = Number(inp.value);
      else params[key] = inp.value.trim();
    }
    await onSave({ name: nameIn.value, kind: kindSel.value, params });
    await refresh();
  });
  const form = el("form");
  form.addEventListener("submit", (e) => e.preventDefault());
  form.appendChild(nameIn); form.appendChild(kindSel); form.appendChild(paramsBox); form.appendChild(save);
  target.appendChild(form);
}
function renderRules(root) {
  const info = el("div", "note",
    "修改规则会产生新版本：旧版本的命中贡献立即撤回（见事件日志），需重新执行后才计入分析。" +
    "迟到的旧版本执行器报告会被明细库拒绝（stale）。");
  root.appendChild(info);
  for (const r of STATE.rules) {
    const card = el("div", "rule-card");
    const head = el("div", "head");
    head.appendChild(chip(r.rule_id));
    head.appendChild(el("span", "name", r.name));
    head.appendChild(el("span", "muted small", `${r.rule_id} · v${r.version}`));
    head.appendChild(stateBadge(r.run_state));
    if (r.exact_count !== null) head.appendChild(el("span", "badge exact", `精确命中 ${fmt(r.exact_count)}`));
    const exec = el("button", null, "执行");
    exec.addEventListener("click", async () => { await api("/api/execute", { rule_id: r.rule_id, executor_id: "exec-ui" }); });
    const del = el("button", "danger", "删除规则");
    del.addEventListener("click", async () => {
      if (confirm(`删除规则 ${r.name}？其全部命中贡献将被撤回。`)) {
        await api(`/api/rules/${r.rule_id}/delete`, {}); await refresh();
      }
    });
    head.appendChild(exec); head.appendChild(del);
    card.appendChild(head);
    const formBox = el("div");
    ruleForm(formBox, { name: r.name, kind: r.definition.kind, params: r.definition.params },
      (body) => api(`/api/rules/${r.rule_id}`, body));
    card.appendChild(formBox);
    root.appendChild(card);
  }
  const add = el("div", "card");
  add.appendChild(el("h3", null, "新增规则"));
  const idIn = el("input"); idIn.placeholder = "rule_id（英文标识）";
  add.appendChild(idIn);
  const formBox = el("div");
  ruleForm(formBox, {}, async (body) => {
    if (!idIn.value.trim()) throw new Error("rule_id 必填");
    await api("/api/rules", { rule_id: idIn.value.trim(), ...body });
  });
  add.appendChild(formBox);
  root.appendChild(add);
}

/* ---------- 重叠组合 ---------- */
function comboRow(c, max) {
  const row = el("div", "bar-row clickable");
  const name = el("div", "name wrap");
  for (const rid of c.rules) name.appendChild(chip(rid));
  row.appendChild(name);
  const track = el("div", "bar-track");
  const bar = el("div", "bar");
  bar.style.width = Math.max(0.5, (c.exact_count / max) * 100) + "%";
  bindTip(bar, fmt(c.exact_count),
    `仅命中 ${c.rules.map(ruleName).join(" + ")} 的记录数（精确）`);
  track.appendChild(bar);
  row.appendChild(track);
  row.appendChild(el("div", "val", fmt(c.exact_count)));
  row.addEventListener("click", () => selectCombo(c.rules));
  return row;
}
async function selectCombo(ruleIds) {
  selectedCombo = ruleIds;
  const box = $("#combo-detail");
  if (!box) return;
  box.replaceChildren(el("div", "muted", "加载中…"));
  const j = await api("/api/overlap?rules=" + ruleIds.join(","));
  box.replaceChildren();
  const h = el("h3", null, "");
  h.textContent = "交集追溯：";
  for (const rid of ruleIds) h.appendChild(chip(rid));
  h.appendChild(el("span", "badge exact", "精确交集"));
  box.appendChild(h);
  box.appendChild(recordTable(j.records));
  if (!j.records.length) box.appendChild(el("div", "muted", "该组合当前没有记录"));
}
function renderOverlap(root) {
  const combos = STATE.live.combinations;
  const card = el("div", "card");
  card.appendChild(el("h2", null, "命中组合（每条记录按其命中的规则集合归类，各行之和 = 去重总体）"));
  const max = Math.max(...combos.map((c) => c.exact_count), 1);
  for (const c of combos) card.appendChild(comboRow(c, max));
  const sum = combos.reduce((a, c) => a + c.exact_count, 0);
  card.appendChild(el("div", "muted small",
    `全部 ${combos.length} 个组合合计 ${fmt(sum)} = 去重总体 ${fmt(STATE.live.overall.union_exact)}（可核对）`));
  root.appendChild(card);
  const detail = el("div", "card");
  detail.id = "combo-detail";
  detail.appendChild(el("div", "muted", "点击上方组合，追溯交集对应的具体记录"));
  root.appendChild(detail);
  if (selectedCombo) selectCombo(selectedCombo);
}

/* ---------- 记录 ---------- */
function recordTable(recs) {
  const t = el("table");
  const head = el("tr");
  for (const h of ["ID", "姓名", "邮箱", "电话", "年龄", "国家", "注册日期", "套餐", "命中规则"])
    head.appendChild(el("th", null, h));
  t.appendChild(head);
  for (const r of recs) {
    const tr = el("tr", "clickable");
    for (const k of ["id", "name", "email", "phone", "age", "country", "signup_date", "plan"])
      tr.appendChild(el("td", null, r[k] ?? "∅"));
    const td = el("td");
    for (const rid of r.hits || []) td.appendChild(chip(rid));
    tr.appendChild(td);
    tr.addEventListener("click", () => showRecord(r.id).catch((e) => alert(e.message)));
    t.appendChild(tr);
  }
  return t;
}
async function showRecord(id) {
  const r = await api("/api/record/" + id);
  const m = $("#modal");
  m.replaceChildren();
  m.appendChild(el("h3", null, `记录 #${r.id}`));
  const t = el("table");
  for (const k of ["name", "email", "phone", "age", "country", "signup_date", "plan"]) {
    const tr = el("tr");
    tr.appendChild(el("th", null, k));
    tr.appendChild(el("td", null, r[k] ?? "∅"));
    t.appendChild(tr);
  }
  m.appendChild(t);
  const h = el("div", "row");
  h.appendChild(el("span", null, "命中："));
  for (const rid of r.hits || []) h.appendChild(chip(rid));
  if (!(r.hits || []).length) h.appendChild(el("span", "muted", "无"));
  m.appendChild(h);
  const close = el("button", null, "关闭");
  close.addEventListener("click", () => $("#modal-bg").style.display = "none");
  m.appendChild(close);
  $("#modal-bg").style.display = "flex";
}
async function renderRecords(root) {
  const j = await api(`/api/records?offset=${recOffset}&limit=50`);
  const card = el("div", "card");
  card.appendChild(el("h2", null, `记录（${fmt(recOffset + 1)}–${fmt(recOffset + j.records.length)} / ${fmt(STATE.record_count)}）`));
  const pager = el("div", "row");
  const prev = el("button", null, "上一页"), next = el("button", null, "下一页");
  prev.disabled = recOffset === 0;
  prev.addEventListener("click", () => { recOffset = Math.max(0, recOffset - 50); render(); });
  next.addEventListener("click", () => { recOffset += 50; render(); });
  pager.appendChild(prev); pager.appendChild(next);
  pager.appendChild(el("span", "muted small", "删除为合法删除：记录本体与全部命中明细被抹除，精确集合同步收缩，HLL 需重建"));
  card.appendChild(pager);
  const t = recordTable(j.records);
  // 追加删除列
  t.rows[0].appendChild(el("th", null, "操作"));
  for (let i = 1; i < t.rows.length; i++) {
    const id = j.records[i - 1].id;
    const td = el("td");
    const btn = el("button", "danger", "删除");
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (confirm(`合法删除记录 #${id}？将抹除其全部命中明细。`)) {
        await api("/api/records/delete", { id }); await refresh();
      }
    });
    td.appendChild(btn);
    t.rows[i].appendChild(td);
  }
  card.appendChild(t);
  root.appendChild(card);
}

/* ---------- 流水线与模拟 ---------- */
function renderPipe(root) {
  const c1 = el("div", "card");
  c1.appendChild(el("h2", null, "执行器（独立进程）"));
  const r1 = el("div", "row");
  const runAll = el("button", "primary", "执行全部规则");
  runAll.addEventListener("click", () => api("/api/execute", { executor_id: "exec-all" }));
  r1.appendChild(runAll);
  const sel = el("select");
  for (const r of STATE.rules) sel.appendChild(el("option", null, `${r.name} (v${r.version})`)).value = r.rule_id;
  const delayIn = el("input"); delayIn.type = "number"; delayIn.value = 8; delayIn.style.width = "70px";
  const slow = el("button", null, "延迟执行该规则");
  slow.addEventListener("click", () => api("/api/execute",
    { rule_id: sel.value, executor_id: "exec-slow", delay: Number(delayIn.value) }));
  const late = el("button", null, "模拟迟到回报");
  late.title = "以旧纪元直接上报，明细库应拒绝（stale）";
  late.addEventListener("click", async () => {
    const r = STATE.rules.find((x) => x.rule_id === sel.value);
    const body = { rule_id: sel.value, executor_id: "exec-late" };
    if (r.version > 1) body.as_rule_version = r.version - 1;
    else if (STATE.input_version > 1) body.as_input_version = STATE.input_version - 1;
    else { alert("当前没有更旧的纪元：请先修改规则或修订输入，再模拟迟到回报"); return; }
    await api("/api/execute", body);
  });
  r1.appendChild(sel); r1.appendChild(el("span", "small muted", "延迟秒数")); r1.appendChild(delayIn);
  r1.appendChild(slow); r1.appendChild(late);
  c1.appendChild(r1);
  c1.appendChild(el("div", "note",
    "「延迟执行」启动一个慢执行器：在它睡眠期间去「规则」页修改同一规则，它的报告到达时会被拒绝为 stale。" +
    "「模拟迟到回报」直接用旧纪元上报，效果立现。"));
  root.appendChild(c1);

  const c2 = el("div", "card");
  c2.appendChild(el("h2", null, "输入与发布"));
  const r2 = el("div", "row");
  const revise = el("button", null, "修订输入（新输入版本）");
  revise.addEventListener("click", async () => { await api("/api/input/revise", {}); await refresh(); });
  const pub = el("button", "primary", "发布分析快照");
  pub.addEventListener("click", () => api("/api/publish", {}));
  const crash = el("button", "danger", "模拟发布中断");
  crash.addEventListener("click", () => api("/api/publish", { crash: true }));
  const recover = el("button", null, "恢复中断的发布");
  recover.addEventListener("click", () => api("/api/publish/recover", {}));
  const reset = el("button", "danger", "重置全部数据");
  reset.addEventListener("click", async () => {
    if (confirm("重置：清空明细库并重新生成虚构数据？")) { await api("/api/reset", {}); await refresh(); }
  });
  r2.appendChild(revise); r2.appendChild(pub); r2.appendChild(crash);
  r2.appendChild(recover); r2.appendChild(reset);
  c2.appendChild(r2);
  c2.appendChild(el("div", "note",
    "发布为两阶段：暂存 → 原子提交。「模拟发布中断」让发布进程在暂存后崩溃（退出码非零），" +
    "已发布快照保持原样；「恢复」清理暂存残留。修订输入会撤回全部命中贡献。"));
  root.appendChild(c2);

  const c3 = el("div", "card");
  c3.appendChild(el("h2", null, "运行记录"));
  const t = el("table");
  const head = el("tr");
  for (const h of ["执行器", "规则", "规则v", "输入v", "状态", "时间"]) head.appendChild(el("th", null, h));
  t.appendChild(head);
  for (const r of STATE.runs.slice(0, 15)) {
    const tr = el("tr");
    tr.appendChild(el("td", null, r.executor_id));
    tr.appendChild(el("td", null, r.rule_id));
    tr.appendChild(el("td", "num", r.rule_version));
    tr.appendChild(el("td", "num", r.input_version));
    const td = el("td"); td.appendChild(stateBadge(r.state)); tr.appendChild(td);
    tr.appendChild(el("td", null, ts(r.reported_at)));
    t.appendChild(tr);
  }
  c3.appendChild(t);
  root.appendChild(c3);

  const c4 = el("div", "card");
  c4.appendChild(el("h2", null, "快照"));
  const st = el("table");
  const sh = el("tr");
  for (const h of ["快照", "状态", "创建", "发布"]) sh.appendChild(el("th", null, h));
  st.appendChild(sh);
  for (const s of STATE.snapshots) {
    const tr = el("tr");
    tr.appendChild(el("td", null, s.snapshot_id));
    const td = el("td");
    const map = { published: ["ok", "已发布"], staging: ["warn", "暂存中"],
                  failed: ["err", "已失败"], superseded: ["dim", "被取代"] };
    const [cls, txt] = map[s.status] || ["dim", s.status];
    td.appendChild(el("span", "badge " + cls, txt));
    tr.appendChild(td);
    tr.appendChild(el("td", null, ts(s.created_at)));
    tr.appendChild(el("td", null, ts(s.published_at)));
    st.appendChild(tr);
  }
  c4.appendChild(st);
  root.appendChild(c4);

  const c5 = el("div", "card");
  c5.appendChild(el("h2", null, "事件日志"));
  const ev = el("div", "events");
  for (const e of STATE.events) {
    const row = el("div", "ev");
    row.appendChild(el("span", "ts", ts(e.ts)));
    row.appendChild(el("span", "kind", e.kind));
    row.appendChild(el("span", null, e.message));
    ev.appendChild(row);
  }
  c5.appendChild(ev);
  root.appendChild(c5);
}

/* ---------- 框架 ---------- */
const RENDERERS = { dash: renderDash, rules: renderRules, overlap: renderOverlap,
                    records: renderRecords, pipe: renderPipe };
async function render() {
  if (!STATE) return;
  $("#hdr-meta").textContent =
    `输入 v${STATE.input_version} · ${STATE.rules.length} 条规则 · ${fmt(STATE.record_count)} 条记录`;
  $("#hdr-pub").textContent = STATE.published ? `已发布 ${STATE.published.snapshot_id}` : "未发布";
  const root = $("#tab-" + TAB);
  root.replaceChildren();
  try { await RENDERERS[TAB](root); }
  catch (e) { root.appendChild(el("div", "note", "渲染失败：" + e.message)); }
}
async function refresh() {
  try { STATE = await api("/api/state"); } catch (e) { return; }
  await render();
}
function switchTab(name) {
  TAB = name;
  for (const b of document.querySelectorAll("#tabs button"))
    b.classList.toggle("on", b.dataset.tab === name);
  for (const sec of document.querySelectorAll("main section"))
    sec.hidden = sec.id !== "tab-" + name;
  render();
}
$("#tabs").addEventListener("click", (e) => {
  if (e.target.dataset.tab) switchTab(e.target.dataset.tab);
});
$("#modal-bg").addEventListener("click", (e) => {
  if (e.target.id === "modal-bg") e.target.style.display = "none";
});
$("#theme-btn").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme;
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("dq-theme", next);
});
const saved = localStorage.getItem("dq-theme");
if (saved) document.documentElement.dataset.theme = saved;
refresh();
setInterval(refresh, 2500);
