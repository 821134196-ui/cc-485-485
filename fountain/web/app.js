/* 喷泉平台前端：原生 JS，定时拉取 /api/state */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const BLUE = "#1f7fb8", GREEN = "#3f8f3f", RED = "#c0473a";
const HOUR0 = 6, HOUR1 = 22; // 日历刻度

let STATE = null;

const role = () => $("input[name=role]:checked").value;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function flash(msg, err = false) {
  const f = $("#flash");
  f.textContent = msg;
  f.className = "flash show" + (err ? " err" : "");
  clearTimeout(flash._t);
  flash._t = setTimeout(() => (f.className = "flash"), 3200);
}

async function act(payload) {
  payload.role = role();
  let res;
  try {
    const r = await fetch("/api/actions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    res = await r.json();
  } catch (e) {
    flash("请求失败：" + e.message, true);
    return null;
  }
  if (!res.ok) flash(res.reason || "操作未成功", true);
  else if (res.reason) flash(res.reason);
  await load();
  return res;
}

async function upload(file, refType, refId, who) {
  const fd = new FormData();
  fd.append("ref_type", refType);
  fd.append("ref_id", refId);
  fd.append("uploaded_by", who);
  fd.append("file", file);
  const r = await fetch("/api/attachments", { method: "POST", body: fd });
  const res = await r.json();
  if (!res.ok) flash(res.reason || "上传失败", true);
  else flash("现场附件已保存");
  await load();
}

// ----------------------------------------------------------------- 总览

function poolState(poolId) {
  return STATE.pool_states.find(s => s.pool_id === poolId) || {};
}
function poolName(id) {
  const p = STATE.pools.find(p => p.id === id);
  return p ? p.name : "#" + id;
}

function renderOverview() {
  const boards = $("#zoneBoards");
  boards.innerHTML = "";
  for (const z of STATE.zones) {
    const board = document.createElement("div");
    board.className = "zone-board";
    board.innerHTML = `<div class="zone-label">${esc(z.name)} · ${esc(z.note || "")}</div>
      <div class="zone-canvas" id="canvas-${z.id}"></div>`;
    boards.appendChild(board);
    const canvas = $(`#canvas-${z.id}`, board);
    for (const p of STATE.pools.filter(p => p.zone_id === z.id)) {
      const st = poolState(p.id);
      const fp = p.footprint;
      const cls = st.lock ? (st.lock.includes("故障") || st.lock.includes("维修") ? "fault" : "locked")
                : st.running ? "running" : "";
      const fig = document.createElement("div");
      fig.className = `pool-figure ${p.shape} ${cls}`;
      fig.style.left = fp.x + "%"; fig.style.top = fp.y + "%";
      fig.style.width = fp.w + "%"; fig.style.height = fp.h + "%";

      const noz = STATE.groups.filter(g => g.pool_id === p.id);
      const lines = noz.flatMap(g => g.position);
      const anyClog = STATE.groups.some(g => g.pool_id === p.id && g.clogged) ||
                      st.telemetry?.clogged;
      fig.innerHTML = `
        <svg viewBox="0 0 100 100" preserveAspectRatio="none"
             style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none">
          ${lines.map(([x1, y1, x2, y2]) =>
            `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"
              class="${anyClog ? "clog" : "on"}"
              vector-effect="non-scaling-stroke"
              stroke="${anyClog ? RED : BLUE}" stroke-width="2.5"
              stroke-linecap="round" opacity="${st.running ? 1 : .55}"/>`).join("")}
        </svg>
        <div class="pool-name">${esc(p.name)}<br>
          <span class="status-pill ${st.running ? "running" : "idle"}">
            ${st.running ? "运行中" : "停喷"}</span></div>`;
      canvas.appendChild(fig);
      canvas.appendChild(buildReadings(p, st, fp));
    }
  }
  renderEvents();
}

function buildReadings(p, st, fp) {
  const t = st.telemetry || {};
  const th = STATE.thresholds;
  const badLvl = t.water_level == null || t.water_level < th.level_min || t.water_level > th.level_max;
  const badTurb = t.turbidity == null || t.turbidity > th.turbidity_max;
  const badCl = t.chlorine == null || t.chlorine < th.chlorine_min || t.chlorine > th.chlorine_max;
  const box = document.createElement("div");
  box.className = "readings-beside";
  // 右侧空间不足时放到池体左侧
  const onLeft = fp.x + fp.w > 72;
  box.style.top = (fp.y + 2) + "%";
  box.style[onLeft ? "left" : "right"] = "auto";
  if (onLeft) {
    box.style.left = Math.max(1, fp.x - 15) + "%";
  } else {
    box.style.left = (fp.x + fp.w + 1.5) + "%";
  }
  box.innerHTML = `
    <div class="rb-title">${esc(p.name)} · 实时读数</div>
    <div class="rb-row"><span>水位</span><b class="${badLvl ? "bad-val" : ""}">${t.water_level?.toFixed(2)} m</b></div>
    <div class="rb-row"><span>浊度</span><b class="${badTurb ? "bad-val" : ""}">${t.turbidity?.toFixed(1)} NTU</b></div>
    <div class="rb-row"><span>余氯</span><b class="${badCl ? "bad-val" : ""}">${t.chlorine?.toFixed(2)} mg/L</b></div>
    ${st.lock ? `<div class="lock-line">⚠ ${esc(st.lock)}</div>` : ""}
    ${st.observation_minutes > 0 ? `<div class="obs-line">投药观察期剩余 ${st.observation_minutes} 分钟</div>` : ""}`;
  return box;
}

function renderEvents() {
  const el = $("#eventList");
  if (!STATE.events.length) { el.innerHTML = `<p class="muted small">暂无事件</p>`; return; }
  el.innerHTML = `<table class="tbl"><thead><tr>
    <th>时间</th><th>水池</th><th>类型</th><th>说明</th><th>处置</th></tr></thead><tbody>
    ${STATE.events.slice(0, 15).map(e => {
      const labels = { low_water: "水位不足", water_exceed: "水质超限", nozzle_clog: "喷头堵塞",
        pump_overheat: "泵体过热", weather_stop: "天气停运", false_start: "误启动拦截",
        chemical_overdose: "药剂超量", visitor_intrusion: "游客进入封控区",
        run_start: "启动", run_stop: "停运", clean_start: "清池", clean_finish: "清池完成",
        fault: "故障", fault_repaired: "维修完成", retest: "水质复测",
        nozzle_trial: "喷头试运行", reopen_request: "复开申请",
        reopen_approved: "复开批准", reopen_returned: "复开退回",
        retest_remind: "复测提醒", refill: "补水", chemical_dose: "药剂投放" };
      return `<tr>
        <td class="small">${esc(e.ts.slice(5, 16))}</td>
        <td>${e.pool_id ? esc(poolName(e.pool_id)) : "—"}</td>
        <td><span class="status-pill ${e.level === "danger" ? "danger" : e.level === "warn" ? "warn" : "ok"}">
          ${esc(labels[e.kind] || e.kind)}</span></td>
        <td>${esc(e.message)}</td>
        <td class="small">${e.resolution
          ? `<span class="muted">${esc(e.handled_by)}：${esc(e.resolution)}</span>`
          : `<button class="btn ghost" onclick="resolveEvent(${e.id})">标记已处置</button>`}</td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

window.resolveEvent = id => act({ action: "resolve_event", id, resolution: "现场处置完成" });

// ----------------------------------------------------------------- 日历

function renderCalendar() {
  const wrap = $("#calendarBands");
  const rows = STATE.schedules.map(s => {
    const sess = STATE.sessions.find(x => x.schedule_id === s.id && x.run_date === STATE.date);
    const status = sess ? sess.status : "idle";
    const [sh, sm] = s.start_at.split(":").map(Number);
    const [eh, em] = s.end_at.split(":").map(Number);
    const left = ((sh + sm / 60 - HOUR0) / (HOUR1 - HOUR0)) * 100;
    const width = Math.max(1.5, ((eh + em / 60) - (sh + sm / 60)) / (HOUR1 - HOUR0) * 100);
    const nowMin = Number(STATE.now.slice(11, 13)) + Number(STATE.now.slice(14, 16)) / 60;
    const nowLeft = ((nowMin - HOUR0) / (HOUR1 - HOUR0)) * 100;
    const reason = sess?.block_reason || sess?.stop_reason || "";
    const isWeather = status === "weather_stop";
    const bandCls = status === "running" ? "" :
      status === "blocked" ? "blocked" : isWeather ? "weather_stop" :
      status === "stopped" && sess?.started_at ? "" : "stopped";
    return `<div class="cal-day">
      <div class="cal-head">
        <span class="pool">${esc(poolName(s.pool_id))}</span>
        <div class="cal-track">
          <div class="cal-band ${bandCls}" style="left:${left}%;width:${width}%"
               title="${esc(reason || s.start_at + "–" + s.end_at)}"></div>
          ${nowLeft >= 0 && nowLeft <= 100 ? `<div class="cal-now" style="left:${nowLeft}%"></div>` : ""}
        </div>
        <span class="cal-times">${s.start_at}–${s.end_at}${s.enabled ? "" : "（已停用）"}</span>
      </div>
      ${reason && status !== "running"
        ? `<div class="cal-reason ${isWeather ? "weather" : ""}">停运原因：${esc(reason)}</div>` : ""}
    </div>`;
  }).join("");
  wrap.innerHTML = `<div class="cal-hours"><span>06:00</span><span>10:00</span><span>14:00</span><span>18:00</span><span>22:00</span></div>${rows}`;

  const hist = STATE.sessions.slice(0, 20);
  $("#calendarHistory").innerHTML = hist.length ? `<table class="tbl">
    <thead><tr><th>日期</th><th>水池</th><th>状态</th><th>启动</th><th>停运</th><th>原因</th></tr></thead>
    <tbody>${hist.map(s => `<tr>
      <td>${esc(s.run_date)}</td><td>${esc(poolName(s.pool_id))}</td>
      <td><span class="status-pill ${s.status === "running" ? "running" :
        s.status === "blocked" ? "danger" : s.status === "weather_stop" ? "warn" : "idle"}">
        ${({ running: "运行中", blocked: "启动阻断", weather_stop: "天气停运", stopped: "已停运" })[s.status]}</span></td>
      <td class="small">${esc(s.started_at?.slice(11, 16) || "—")}</td>
      <td class="small">${esc(s.stopped_at?.slice(11, 16) || "—")}</td>
      <td class="small">${esc(s.block_reason || s.stop_reason || "")}</td>
    </tr>`).join("")}</tbody></table>` : `<p class="muted small">暂无运行记录</p>`;
}

// ----------------------------------------------------------------- 水质趋势

function lineChart(samples, opt) {
  const W = 760, H = 170, PL = 38, PR = 132, PT = 14, PB = 26;
  const iw = W - PL - PR, ih = H - PT - PB;
  const vals = samples.map(s => s[opt.key]);
  let lo = Math.min(...vals, ...(opt.thresholds || []).map(t => t.value)) - opt.pad;
  let hi = Math.max(...vals, ...(opt.thresholds || []).map(t => t.value)) + opt.pad;
  const t0 = new Date(samples[0].ts + ":00").getTime();
  const t1 = new Date(samples[samples.length - 1].ts + ":00").getTime();
  const span = Math.max(1, t1 - t0);
  const X = ts => PL + (new Date(ts + ":00").getTime() - t0) / span * iw;
  const Y = v => PT + (hi - v) / (hi - lo) * ih;
  const bad = v => opt.thresholds.some(t =>
    (t.dir === "max" && v > t.value) || (t.dir === "min" && v < t.value) ||
    (t.dir === "range" && (v < t.lo || v > t.hi)));

  const pts = samples.map(s => `${X(s.ts).toFixed(1)},${Y(s[opt.key]).toFixed(1)}`).join(" ");
  const grid = [0, .25, .5, .75, 1].map(f => {
    const y = PT + f * ih, v = hi - f * (hi - lo);
    return `<line class="chart-grid" x1="${PL}" y1="${y}" x2="${W - PR}" y2="${y}"/>
      <text class="chart-label" x="${PL - 5}" y="${y + 3}" text-anchor="end">${v.toFixed(opt.tick || 1)}</text>`;
  }).join("");
  const ticks = [0, .25, .5, .75, 1].map(f => {
    const tt = new Date(t0 + f * span);
    const lab = `${String(tt.getHours()).padStart(2, "0")}:${String(tt.getMinutes()).padStart(2, "0")}`;
    return `<text class="chart-label" x="${PL + f * iw}" y="${H - 8}" text-anchor="middle">${lab}</text>`;
  }).join("");
  const thresh = (opt.thresholds || []).flatMap(t => {
    const ys = t.dir === "range" ? [Y(t.lo), Y(t.hi)] : [Y(t.value)];
    const labels = t.dir === "range" ? [`下限 ${t.lo}`, `上限 ${t.hi}`] : [t.label];
    return ys.map((y, i) => `<line class="chart-thresh" x1="${PL}" y1="${y}" x2="${W - PR + 4}" y2="${y}"/>
      <text class="chart-thresh-label" x="${W - PR + 8}" y="${y + 3}">${esc(labels[i])}</text>`);
  }).join("");
  const dots = samples.map(s => {
    const b = bad(s[opt.key]);
    return `<circle class="chart-dot" cx="${X(s.ts)}" cy="${Y(s[opt.key])}" r="${b ? 3.6 : 2.4}"
      fill="${b ? RED : opt.color}" data-tip="${esc(s.ts.slice(5))}｜${opt.name} ${s[opt.key].toFixed(opt.tick || 1)} ${opt.unit}${b ? " · 越限" : ""}"/>`;
  }).join("");

  return `<div class="chart-title">${opt.title}（${opt.unit}）</div>
  <svg viewBox="0 0 ${W} ${H}" data-chart>
    ${grid}
    <line class="chart-axis" x1="${PL}" y1="${PT}" x2="${PL}" y2="${H - PB}"/>
    <line class="chart-axis" x1="${PL}" y1="${H - PB}" x2="${W - PR}" y2="${H - PB}"/>
    ${ticks}${thresh}
    <polyline class="chart-line" stroke="${opt.color}" points="${pts}"/>
    ${dots}
    <line class="crosshair" x1="0" y1="${PT}" x2="0" y2="${H - PB}"
      stroke="#56534c" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/>
  </svg>`;
}

async function renderWater() {
  const pid = $("#waterPool").value;
  const hours = $("#waterHours").value;
  const r = await fetch(`/api/pools/${pid}/samples?hours=${hours}`);
  const { samples } = await r.json();
  const th = STATE.thresholds;
  const el = $("#waterCharts");
  if (samples.length < 2) { el.innerHTML = `<p class="muted small">采样点不足，等待适配器读数…</p>`; }
  else {
    el.innerHTML =
      lineChart(samples, { key: "water_level", name: "水位", title: "水位", unit: "m",
        color: BLUE, tick: 2, pad: .12,
        thresholds: [{ dir: "range", lo: th.level_min, hi: th.level_max, label: "" }] })
        .replace(`下限 ${th.level_min}`, "").replace(`上限 ${th.level_max}`, "") +
      lineChart(samples, { key: "turbidity", name: "浊度", title: "浊度", unit: "NTU",
        color: BLUE, tick: 1, pad: .6,
        thresholds: [{ dir: "max", value: th.turbidity_max, label: `上限 ${th.turbidity_max}` }] }) +
      lineChart(samples, { key: "chlorine", name: "余氯", title: "余氯", unit: "mg/L",
        color: GREEN, tick: 2, pad: .08,
        thresholds: [{ dir: "range", lo: th.chlorine_min, hi: th.chlorine_max, label: "" }] });
    bindChartHover();
  }
  $("#waterMeta").textContent = `共 ${samples.length} 条采样`;
  $("#latestSamples").innerHTML = STATE.pools.map(p => {
    const s = STATE.latest_samples[String(p.id)] || STATE.latest_samples[p.id];
    if (!s) return `<tr><td>${esc(p.name)}</td><td colspan="4" class="muted">无</td></tr>`;
    const bL = s.water_level < th.level_min || s.water_level > th.level_max;
    const bT = s.turbidity > th.turbidity_max;
    const bC = s.chlorine < th.chlorine_min || s.chlorine > th.chlorine_max;
    return `<tr><td>${esc(p.name)}</td>
      <td class="num ${bL ? "bad-val" : ""}">${s.water_level.toFixed(2)}</td>
      <td class="num ${bT ? "bad-val" : ""}">${s.turbidity.toFixed(1)}</td>
      <td class="num ${bC ? "bad-val" : ""}">${s.chlorine.toFixed(2)}</td>
      <td class="small muted">${esc(s.ts.slice(5, 16))} · ${sourceLabel(s.source)}</td></tr>`;
  }).join("");
}

const sourceLabel = s => ({ adapter: "适配器", manual: "人工", retest: "复测" }[s] || s);

function bindChartHover() {
  const tip = $("#tooltip");
  $$("svg[data-chart]").forEach(svg => {
    const line = $(".crosshair", svg);
    svg.addEventListener("mousemove", ev => {
      const rect = svg.getBoundingClientRect();
      const x = (ev.clientX - rect.left) / rect.width * 760;
      const dots = $$("circle[data-tip]", svg);
      let best = null, bd = 1e9;
      dots.forEach(d => {
        const dd = Math.abs(d.getAttribute("cx") - x);
        if (dd < bd) { bd = dd; best = d; }
      });
      if (best) {
        line.setAttribute("x1", best.getAttribute("cx"));
        line.setAttribute("x2", best.getAttribute("cx"));
        line.setAttribute("visibility", "visible");
        tip.textContent = best.dataset.tip;
        tip.style.display = "block";
        tip.style.left = (ev.clientX + 12) + "px";
        tip.style.top = (ev.clientY + 12) + "px";
      }
    });
    svg.addEventListener("mouseleave", () => {
      line.setAttribute("visibility", "hidden");
      tip.style.display = "none";
    });
  });
}

// ----------------------------------------------------------------- 泵组

function renderPumps() {
  $("#pumpCards").innerHTML = STATE.pumps.map(pu => {
    const st = poolState(pu.pool_id);
    const t = st.telemetry?.pump_temp ?? pu.temperature;
    const hot = t > STATE.thresholds.pump_temp_max;
    const stLabel = { idle: "备用", running: "运行中", fault: "故障", maintenance: "保养锁定" }[pu.status];
    return `<div class="card">
      <h3>${esc(pu.code)} · ${esc(pu.name)}
        <span class="status-pill ${pu.status}" style="float:right">${stLabel}</span></h3>
      <div class="rb-row"><span>所属水池</span><b>${esc(poolName(pu.pool_id))}</b></div>
      <div class="rb-row"><span>泵体温度</span><b class="${hot ? "bad-val" : ""}">${t.toFixed(0)} ℃</b></div>
      <div class="hint small">${pu.fault_code ? "故障码：" + esc(pu.fault_code) :
        (hot ? "超过过热阈值 75 ℃，应立即停运检修" : "温度正常（阈值 75 ℃）")}</div>
    </div>`;
  }).join("");
}

// ----------------------------------------------------------------- 药剂

function renderChemical() {
  $("#chemicalRows").innerHTML = STATE.chemicals.length ? STATE.chemicals.map(c => `
    <tr><td class="small">${esc(c.ts.slice(5, 16))}</td>
      <td>${esc(poolName(c.pool_id))}</td><td>${esc(c.agent)}</td>
      <td class="num ${c.overdose ? "bad-val" : ""}">${c.amount} ${c.unit}</td>
      <td class="small">${esc(c.observe_until.slice(5, 16))}</td>
      <td>${esc(c.operator)}</td>
      <td>${c.overdose ? `<span class="status-pill danger">超量·待复测</span>`
        : `<span class="status-pill ok">观察期</span>`}</td></tr>`).join("")
    : `<tr><td colspan="7" class="muted">暂无投放记录</td></tr>`;
  $("#refillRows").innerHTML = STATE.refills.length ? STATE.refills.map(r => `
    <tr><td class="small">${esc(r.ts.slice(5, 16))}</td><td>${esc(poolName(r.pool_id))}</td>
      <td class="num">${r.amount_liters.toFixed(0)}</td><td>${esc(r.operator)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted">暂无补水记录</td></tr>`;
}

// ----------------------------------------------------------------- 清池 / 故障

function stepper(steps, activeIdx) {
  return `<div class="stepper">${steps.map((s, i) =>
    `<div class="step ${i < activeIdx ? "done" : i === activeIdx ? "active" : ""}">${s}</div>`
  ).join("")}</div>`;
}

function attachmentsHtml(refType, refId) {
  const items = STATE.attachments.filter(a => a.ref_type === refType && a.ref_id === refId);
  return `<div class="attach-list">
    ${items.map(a => `<a href="/attachments/${encodeURIComponent(a.stored_name)}" target="_blank">📎 ${esc(a.filename)}</a>`).join("")}
    <label class="btn gray small" style="display:inline-block">上传现场照片
      <input type="file" hidden onchange="onAttach(this,'${refType}',${refId})"></label>
  </div>`;
}
window.onAttach = (input, refType, refId) => {
  const f = input.files[0];
  if (f) upload(f, refType, refId, role() === "supervisor" ? "主管" : "养护员");
};

function renderClean() {
  const isSup = role() === "supervisor";
  $("#cleanCards").innerHTML = STATE.cleans.length ? STATE.cleans.map(c => {
    const steps = ["开始清池", "作业完成", "水质复测", "喷头试运行", "主管复开"];
    let idx = c.status === "active" ? 1 : c.status === "awaiting_retest"
      ? 1 + c.retest_passed + c.nozzle_trial_passed : 5;
    let actions = "";
    if (c.status === "active")
      actions = `<button class="btn green" onclick="act({action:'clean_finish',id:${c.id}}).then(load)">清池作业完成</button>`;
    if (c.status === "awaiting_retest") {
      actions = `${!c.retest_passed ? `<button class="btn ghost" onclick="act({action:'retest',pool_id:${c.pool_id}}).then(load)">水质复测</button>` : ""}
        ${c.retest_passed && !c.nozzle_trial_passed ? `<button class="btn ghost" onclick="act({action:'nozzle_trial',pool_id:${c.pool_id}}).then(load)">喷头试运行</button>` : ""}
        ${c.retest_passed && c.nozzle_trial_passed ? `<button class="btn" onclick="act({action:'reopen_request',ref_type:'clean',ref_id:${c.id}}).then(load)">提交复开申请</button>` : ""}
        <span class="hint small">两项均通过后才能提交主管复开</span>`;
    }
    if (c.status === "done") actions = `<span class="status-pill ok">已复开</span>`;
    return `<div class="card"><h3>${esc(poolName(c.pool_id))} · 清池任务 #${c.id}
        <span class="status-pill ${c.status === "active" ? "warn" : c.status === "done" ? "ok" : "idle"}" style="float:right">
        ${{ active: "锁定中", awaiting_retest: "待复测复开", done: "已完成", cancelled: "已取消" }[c.status]}</span></h3>
      ${stepper(steps, idx)}
      <div class="small muted">开始 ${esc(c.started_at.slice(5, 16))}${c.finished_at ? " ｜ 完成 " + esc(c.finished_at.slice(5, 16)) : ""} ｜ 养护员 ${esc(c.keeper)}</div>
      ${c.note ? `<div class="small">${esc(c.note)}</div>` : ""}
      <div class="row-actions" style="margin-top:8px">${actions}</div>
      ${attachmentsHtml("clean", c.id)}</div>`;
  }).join("") : `<p class="muted small">暂无清池任务</p>`;
}

function renderFault() {
  $("#faultCards").innerHTML = STATE.faults.length ? STATE.faults.map(f => {
    const steps = ["故障停运", "维修完成", "水质复测", "喷头试运行", "主管复开"];
    let idx = f.status === "open" ? 1 : f.status === "repairing" ? 1
      : f.status === "awaiting_retest" ? 1 + f.retest_passed + f.nozzle_trial_passed : 5;
    let actions = "";
    if (f.status === "open" || f.status === "repairing")
      actions = `<button class="btn green" onclick="act({action:'fault_repair',id:${f.id},note:'现场修复完成'}).then(load)">维修完成</button>`;
    if (f.status === "awaiting_retest") {
      actions = `${!f.retest_passed ? `<button class="btn ghost" onclick="act({action:'retest',pool_id:${f.pool_id}}).then(load)">水质复测</button>` : ""}
        ${f.retest_passed && !f.nozzle_trial_passed ? `<button class="btn ghost" onclick="act({action:'nozzle_trial',pool_id:${f.pool_id}}).then(load)">喷头试运行</button>` : ""}
        ${f.retest_passed && f.nozzle_trial_passed ? `<button class="btn" onclick="act({action:'reopen_request',ref_type:'fault',ref_id:${f.id}}).then(load)">提交复开申请</button>` : ""}`;
    }
    if (f.status === "resolved") actions = `<span class="status-pill ok">已复开</span>`;
    const kind = { nozzle_clog: "喷头堵塞", pump_overheat: "泵体过热", other: "其他" }[f.kind];
    return `<div class="card"><h3>${esc(poolName(f.pool_id))} · 故障单 #${f.id}
        <span class="status-pill ${f.status === "resolved" ? "ok" : "danger"}" style="float:right">
        ${{ open: "待维修", repairing: "维修中", awaiting_retest: "待复测复开", resolved: "已解决" }[f.status]}</span></h3>
      ${stepper(steps, idx)}
      <div><b>${kind}</b> <span class="small muted">${esc(f.ts.slice(5, 16))}</span></div>
      <div class="small">${esc(f.detail)}${f.repair_note ? " ｜ 维修：" + esc(f.repair_note) : ""}</div>
      <div class="row-actions" style="margin-top:8px">${actions}</div>
      ${attachmentsHtml("fault", f.id)}</div>`;
  }).join("") : `<p class="muted small">暂无故障单</p>`;
}

// ----------------------------------------------------------------- 复开审批

function renderReopen() {
  const isSup = role() === "supervisor";
  $("#reopenCards").innerHTML = STATE.reopen_requests.length ? STATE.reopen_requests.map(r => {
    const refLabel = r.ref_type === "clean" ? `清池任务 #${r.ref_id}` : `故障维修 #${r.ref_id}`;
    const pill = { pending: "warn", approved: "ok", rejected: "danger" }[r.status];
    const stLabel = { pending: "待主管审批", approved: "已批准复开", rejected: "已退回" }[r.status];
    return `<div class="card"><h3>${esc(poolName(r.pool_id))} · ${refLabel}
        <span class="status-pill ${pill}" style="float:right">${stLabel}</span></h3>
      <div class="small muted">申请人 ${esc(r.requested_by)} ｜ ${esc(r.requested_at.slice(5, 16))}</div>
      ${r.status === "pending" ? `
        <div class="row-actions" style="margin-top:8px">
          <button class="btn green" ${!isSup ? "disabled title='仅主管可批准'" : ""}
            onclick="act({action:'approve',id:${r.id}}).then(load)">批准复开</button>
          <button class="btn red" ${!isSup ? "disabled" : ""}
            onclick="act({action:'reject',id:${r.id},reason:'复测条件不满足'}).then(load)">退回</button>
          ${!isSup ? `<span class="hint small">养护员无审批权限，请切换到主管身份</span>` : ""}
        </div>` :
        `<div class="small">审批人：${esc(r.reviewer || "—")} ${esc(r.reviewed_at?.slice(5, 16) || "")}</div>
         ${r.reject_reason ? `<div class="bad-val small">${esc(r.reject_reason)}</div>` : ""}`}
    </div>`;
  }).join("") : `<p class="muted small">暂无复开申请</p>`;
}

// ----------------------------------------------------------------- 表单 / 初始化

function fillSelects() {
  const opts = STATE.pools.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  $$("select.pool-select").forEach(s => {
    const cur = s.value;
    s.innerHTML = opts;
    if (cur) s.value = cur;
  });
  const sOpts = STATE.schedules.map(s =>
    `<option value="${s.id}">${esc(poolName(s.pool_id))} ${s.start_at}–${s.end_at}</option>`).join("");
  $("#startSched").innerHTML = sOpts;
}

function bindForms() {
  const wire = (id, action, extra = {}) =>
    $("#" + id).addEventListener("submit", async e => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const payload = { action, ...Object.fromEntries(fd), ...extra };
      for (const k of ["pool_id", "schedule_id", "id", "ref_id"])
        if (payload[k] != null) payload[k] = Number(payload[k]);
      for (const k of ["amount", "water_level", "turbidity", "chlorine", "wind_level"])
        if (payload[k] != null) payload[k] = Number(payload[k]);
      await act(payload);
      e.target.reset?.();
    });
  wire("form-start", "start");
  wire("form-refill", "refill");
  wire("form-visitor", "visitor");
  wire("form-scenario", "sim_scenario");
  wire("form-weather", "sim_weather");
  wire("form-sample", "sample");
  wire("form-chemical", "chemical");
  wire("form-clean", "clean_start");
  wire("form-fault", "fault_report");

  $("#waterPool").addEventListener("change", renderWater);
  $("#waterHours").addEventListener("change", renderWater);
  $$("input[name=role]").forEach(r => r.addEventListener("change", renderAll));
  $$("#tabs button").forEach(b => b.addEventListener("click", () => {
    $$("#tabs button").forEach(x => x.classList.toggle("active", x === b));
    $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + b.dataset.view));
    if (b.dataset.view === "water") renderWater();
  }));
}

function renderHeader() {
  $("#clock").textContent = STATE.now;
  const w = STATE.weather;
  const names = { clear: "晴", cloudy: "多云", heavy_rain: "暴雨", storm: "雷暴", typhoon: "台风" };
  const chip = $("#weatherChip");
  chip.textContent = `${names[w.code] || w.code} ${w.temp_c}℃ · ${w.wind_level}级风` +
    (STATE.weather_block ? " ⚠ " + STATE.weather_block : "");
  chip.className = "weather-chip" + (STATE.weather_block ? " stop" : "");
}

function renderAll() {
  renderHeader();
  fillSelects();
  renderOverview();
  renderCalendar();
  renderPumps();
  renderChemical();
  renderClean();
  renderFault();
  renderReopen();
  if ($("#view-water").classList.contains("active")) renderWater();
}

async function load() {
  const r = await fetch("/api/state");
  STATE = await r.json();
  renderAll();
}

window.act = act;
window.load = load;

bindForms();
load();
setInterval(load, 10000);
