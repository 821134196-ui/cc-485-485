/* 公园喷泉平台前端：原生 JS，无依赖。 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  constants: null, pools: [], zones: [], role: 'keeper',
  currentPool: 1, timer: null,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body instanceof FormData ? opts.body
      : opts.body ? JSON.stringify({ role: state.role, ...opts.body }) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, isErr = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = `toast show${isErr ? ' err' : ''}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = 'toast'), 3200);
}
const fail = (e) => toast(e.message || String(e), true);

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function tag(text, cls) { return `<span class="tag ${cls}">${esc(text)}</span>`; }
function timeShort(t) { return esc((t || '').slice(5, 16)); }

const STATUS_TAG = {
  idle: ['停止', 'gray'], running: ['运行中', 'blue'], fault: ['故障', 'red'], repair: ['维修中', 'amber'],
};
const CLEAN_TAG = {
  planned: ['待锁定', 'gray'], locked: ['已锁定', 'amber'], in_progress: ['清池中', 'amber'], done: ['待复开', 'blue'],
};
const REOPEN_TAG = { pending: ['待审批', 'amber'], approved: ['已批准', 'green'], rejected: ['已退回', 'red'] };
const FAULT_TAG = { open: ['待处理', 'red'], repairing: ['维修中', 'amber'], resolved: ['已闭环', 'green'], rejected: ['退回', 'red'] };

/* ---------------- 页签 ---------------- */
$$('#tabs button').forEach(b => b.addEventListener('click', () => {
  $$('#tabs button').forEach(x => x.classList.toggle('active', x === b));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${b.dataset.view}`));
  refresh();
}));
$('#roleSelect').addEventListener('change', e => { state.role = e.target.value; refresh(); });

/* ---------------- 总览 SVG ---------------- */
function renderOverview(data) {
  const map = $('#overviewMap');
  map.innerHTML = '';
  data.zones.forEach(zone => {
    const card = document.createElement('div');
    card.className = 'zone-card';
    const pool = zone.pools[0];
    const locked = !!pool.cleaning;
    const fault = pool.faults.length > 0;
    const ringCls = pool.running ? 'run' : fault ? 'fault' : locked ? 'locked' : 'idle';
    const stroke = pool.running ? '#2E86C1' : fault ? '#C0392B' : locked ? '#D98E04' : '#9AA1A8';
    const waterFill = pool.running ? '#D6EAF6' : fault ? '#F7E4E1' : locked ? '#FBF0DB' : '#EAE7DF';

    let shape = '';
    if (zone.outline === 'circle') {
      shape = `<circle cx="150" cy="150" r="105" fill="${waterFill}" stroke="${stroke}" stroke-width="3"/>
               <circle cx="150" cy="150" r="105" fill="none" stroke="#4C9A57" stroke-width="1.5"
                 stroke-dasharray="2 7" opacity="0.8"/>`;
    } else {
      shape = `<polygon points="${zone.outline}" fill="${waterFill}" stroke="${stroke}" stroke-width="3"/>
               <polygon points="${zone.outline}" fill="none" stroke="#4C9A57" stroke-width="1.5"
                 stroke-dasharray="2 7" opacity="0.8"/>`;
    }
    // 喷头组：池体上的短线（小竖线 + 水花点）
    let nozzles = '';
    pool.nozzle_groups.forEach(g => {
      g.layout.split(';').forEach(pt => {
        const [x, y] = pt.split(',').map(Number);
        const blocked = g.blocked;
        const c = blocked ? '#C0392B' : pool.running ? '#2E86C1' : '#7FAECB';
        nozzles += `<line x1="${x}" y1="${y - 9}" x2="${x}" y2="${y + 1}" stroke="${c}" stroke-width="2.5" stroke-linecap="round"/>
                    <circle cx="${x}" cy="${y - 12}" r="${pool.running && !blocked ? 2.4 : 1.6}"
                      fill="${c}" opacity="${pool.running ? 0.9 : 0.5}"/>`;
      });
    });
    // 水质数值放在池体旁（不是独立仪表）
    const r = pool.last_reading || {};
    const bad = (v, cond) => cond ? 'bad' : '';
    const th = state.constants.thresholds;
    const readTxt = `
      <text x="16" y="284" class="pool-value ${bad(r.level, r.level < th.water_level_min)}">水位 ${(r.level * 100).toFixed(0)}%</text>
      <text x="100" y="284" class="pool-value ${bad(r.turbidity, r.turbidity > th.turbidity_max)}">浊度 ${Number(r.turbidity).toFixed(1)} NTU</text>
      <text x="206" y="284" class="pool-value ${bad(r.chlorine, r.chlorine < th.chlorine_min || r.chlorine > th.chlorine_max)}">余氯 ${Number(r.chlorine).toFixed(2)}</text>`;

    let stateTxt = '';
    if (pool.running) stateTxt = `<text x="150" y="156" text-anchor="middle" class="pool-state" fill="#1B5E8C">运行中</text>`;
    else if (locked) stateTxt = `<text x="150" y="150" text-anchor="middle" class="pool-state" fill="#D98E04">清池锁定</text>
                                 <text x="150" y="166" text-anchor="middle" class="pool-state" fill="#D98E04">计划暂停</text>`;
    else if (fault) stateTxt = `<text x="150" y="150" text-anchor="middle" class="pool-state" fill="#C0392B">故障停运</text>
                                <text x="150" y="166" text-anchor="middle" class="pool-state" fill="#C0392B">${esc(state.constants.fault_text[pool.faults[0].kind] || '')}</text>`;
    else stateTxt = `<text x="150" y="156" text-anchor="middle" class="pool-state" fill="#6E747B">待机</text>`;

    if (pool.observing) {
      stateTxt += `<text x="150" y="262" text-anchor="middle" class="pool-state" fill="#D98E04">药剂观察期至 ${esc(pool.observing.observe_until.slice(11, 16))}</text>`;
    }

    card.innerHTML = `
      <div class="zone-title">${esc(zone.name)} · ${esc(pool.name)}</div>
      <svg width="300" height="292" viewBox="0 0 300 292" role="img" aria-label="${esc(pool.name)}状态图">
        ${shape}${nozzles}${stateTxt}
        <text x="16" y="270" class="pool-label">水质</text>${readTxt}
      </svg>
      <div style="text-align:center">
        <button class="btn small" data-start="${pool.id}" ${pool.running ? 'disabled' : ''}>启动</button>
        <button class="btn small ghost" data-stop="${pool.id}" ${!pool.running ? 'disabled' : ''}>停止</button>
      </div>`;
    map.appendChild(card);
  });

  $$('#overviewMap [data-start]').forEach(b => b.onclick = async () => {
    try {
      const res = await api('/api/pools/start', { method: 'POST', body: { pool_id: +b.dataset.start, manual: true } });
      if (res.idempotent) toast('已在运行，启动请求合并（幂等）');
      else toast(res.ok ? '启动成功' : '启动被拦截');
    } catch (e) { fail(e); }
    refresh();
  });
  $$('#overviewMap [data-stop]').forEach(b => b.onclick = () =>
    api('/api/pools/stop', { method: 'POST', body: { pool_id: +b.dataset.stop } })
      .then(() => toast('已停止')).catch(fail).then(refresh));
}

/* ---------------- 运行日历时间带 ---------------- */
function renderCalendar(schedules, logs) {
  $('#calDate').textContent = new Date().toLocaleDateString('zh-CN', { weekday: 'long', month: 'long', day: 'numeric' });
  const START_H = 6, END_H = 22; // 安静时间带 06:00–22:00
  const pct = hm => {
    const [h, m] = hm.split(':').map(Number);
    return ((h + m / 60 - START_H) / (END_H - START_H)) * 100;
  };
  const nowPct = (() => {
    const d = new Date();
    return pct(`${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`);
  })();

  const byPool = {};
  schedules.forEach(s => (byPool[s.pool_id] ||= []).push(s));
  const band = $('#calendarBand');
  band.innerHTML = `<div class="band-axis"><span>06:00</span><span>10:00</span><span>14:00</span><span>18:00</span><span>22:00</span></div>`;
  state.pools.forEach(p => {
    const blocks = (byPool[p.id] || []).map(s => {
      const left = Math.max(0, pct(s.start_at));
      const width = Math.min(100, pct(s.end_at)) - left;
      return `<div class="band-block" style="left:${left}%;width:${width}%"
        title="${esc(s.start_at)}-${esc(s.end_at)} 周${esc(s.days)}">${esc(s.start_at)}-${esc(s.end_at)}</div>`;
    }).join('');
    const nowMark = nowPct >= 0 && nowPct <= 100 ? `<div class="band-now" style="left:${nowPct}%"></div>` : '';
    band.innerHTML += `
      <div class="band-row">
        <div>${esc(p.name)}</div>
        <div class="band-track">${blocks}${nowMark}</div>
        <div>${latestReason(logs, p.id)}</div>
      </div>`;
  });

  $('#scheduleTable').innerHTML = `
    <tr><th>水池</th><th>时段</th><th>星期</th><th>状态</th><th>上次执行</th><th></th></tr>
    ${schedules.map(s => `<tr>
      <td>${esc(s.pool_name)}</td><td>${esc(s.start_at)}–${esc(s.end_at)}</td>
      <td>周${[...s.days].sort().map(d => '一二三四五六日'[+d - 1]).join('')}</td>
      <td>${s.enabled ? tag('启用', 'green') : tag('停用', 'gray')}</td>
      <td>${esc(s.last_run_date || '—')}</td>
      <td><button class="btn small ghost" data-toggle="${s.id}">${s.enabled ? '停用' : '启用'}</button></td>
    </tr>`).join('')}
    <tr><td colspan="6" class="muted">同一水池时段重叠的计划会被拒绝；清池/故障期间到点启动将被前置校验拦截并在日志中说明原因。</td></tr>`;
  $$('#scheduleTable [data-toggle]').forEach(b => b.onclick = () =>
    api(`/api/schedules/${b.dataset.toggle}/toggle`, { method: 'POST' }).catch(fail).then(refresh));

  $('#logsTable').innerHTML = `
    <tr><th>时间</th><th>水池</th><th>动作</th><th>结果</th><th>原因（停运以文本说明）</th><th>操作人</th></tr>
    ${logs.map(l => `<tr>
      <td>${timeShort(l.ts)}</td><td>${esc(l.pool_name)}</td>
      <td>${({ start: '启动', stop: '停止', block: '拦截', abort: '紧急停运', nozzle_trial: '喷头试运行' })[l.action] || l.action}</td>
      <td>${l.result === 'ok' ? tag('成功', 'green') : l.result === 'blocked' ? tag('阻断', 'amber') : tag('未执行', 'red')}</td>
      <td>${esc(l.reason || '')}</td><td>${esc(l.actor)}</td>
    </tr>`).join('')}`;
}

function latestReason(logs, poolId) {
  const l = logs.find(x => x.pool_id === poolId && (x.action === 'abort' || x.action === 'stop' || x.action === 'block'));
  if (!l) return '<span class="muted">—</span>';
  if (l.action === 'block') return `<span class="stop-reason">启动被拦截：${esc(l.reason)}</span>`;
  return `<span class="stop-reason ${l.result === 'ok' && l.action === 'stop' ? '' : 'green'}">${esc(l.reason)}</span>`;
}

/* ---------------- 水质趋势图（只保留必要阈值） ---------------- */
function lineChart(series, opts) {
  const W = 320, H = 150, PAD = { l: 34, r: 10, t: 12, b: 20 };
  const iw = W - PAD.l - PAD.r, ih = H - PAD.t - PAD.b;
  const xs = series.map((_, i) => PAD.l + (series.length === 1 ? iw / 2 : i * iw / (series.length - 1)));
  const y = v => PAD.t + ih - ((v - opts.min) / (opts.max - opts.min)) * ih;
  const path = series.map((v, i) => `${i ? 'L' : 'M'}${xs[i].toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  const area = `${path} L${xs[xs.length - 1].toFixed(1)},${PAD.t + ih} L${xs[0].toFixed(1)},${PAD.t + ih} Z`;
  const thLine = v => `<line class="threshold ${opts.greenTh ? 'green-th' : ''}" x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}"/>
    <text class="axis-text" x="${W - PAD.r}" y="${y(v) - 2}" text-anchor="end">阈值 ${v}</text>`;
  const gridY = [opts.min, (opts.min + opts.max) / 2, opts.max].map(v =>
    `<line x1="${PAD.l}" x2="${W - PAD.r}" y1="${y(v)}" y2="${y(v)}" stroke="#EEECE6"/>
     <text class="axis-text" x="${PAD.l - 4}" y="${y(v) + 3}" text-anchor="end">${v}</text>`).join('');
  const dots = series.map((v, i) =>
    `<circle cx="${xs[i]}" cy="${y(v)}" r="3" fill="${opts.color}" stroke="#fff" stroke-width="1">
      <title>${esc(opts.labels[i])}　${v}${opts.unit}</title></circle>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}">
    ${gridY}
    ${(opts.thresholds || []).map(thLine).join('')}
    <path d="${area}" class="area-blue"/>
    <path d="${path}" class="${opts.cls}" stroke="${opts.color}"/>
    ${dots}
    <text class="axis-text" x="${PAD.l}" y="${H - 6}">${esc(opts.labels[0]?.slice(5, 10) || '')}</text>
    <text class="axis-text" x="${W - PAD.r}" y="${H - 6}" text-anchor="end">${esc(opts.labels[opts.labels.length - 1]?.slice(5, 10) || '')}</text>
  </svg>`;
}

async function renderWater() {
  const pid = +$('#waterPool').value;
  state.currentPool = pid;
  const hours = +$('#waterHours').value;
  const rows = await api(`/api/pools/${pid}/readings?hours=${hours}`);
  const th = state.constants.thresholds;
  const labels = rows.map(r => r.ts);
  $('#waterCharts').innerHTML = `
    <div class="chart-box"><h5>水位（占池深）</h5>
      ${lineChart(rows.map(r => r.level), { min: 0.4, max: 1, color: '#2E86C1', cls: 'series-blue', unit: '', labels, thresholds: [th.water_level_min] })}
    </div>
    <div class="chart-box"><h5>浊度 NTU（上限 ${th.turbidity_max}）</h5>
      ${lineChart(rows.map(r => r.turbidity), { min: 0, max: Math.max(10, ...rows.map(r => r.turbidity)), color: '#2E86C1', cls: 'series-blue', unit: 'NTU', labels, thresholds: [th.turbidity_max] })}
    </div>
    <div class="chart-box"><h5>余氯 mg/L（${th.chlorine_min}–${th.chlorine_max}）</h5>
      ${lineChart(rows.map(r => r.chlorine), { min: 0, max: 1.4, color: '#4C9A57', cls: 'series-green', unit: 'mg/L', labels, thresholds: [th.chlorine_min, th.chlorine_max], greenTh: true })}
    </div>`;
  $('#readingsTable').innerHTML = `
    <tr><th>时间</th><th>类型</th><th>水位</th><th>浊度 NTU</th><th>余氯 mg/L</th><th>判定</th><th>备注</th></tr>
    ${rows.slice().reverse().slice(0, 20).map(r => {
      const ok = r.level >= th.water_level_min && r.turbidity <= th.turbidity_max &&
        r.chlorine >= th.chlorine_min && r.chlorine <= th.chlorine_max;
      return `<tr><td>${esc(r.ts)}</td>
        <td>${({ auto: '自动采样', manual: '人工', retest: '复测' })[r.kind] || r.kind}</td>
        <td>${(r.level * 100).toFixed(0)}%</td><td>${r.turbidity.toFixed(1)}</td><td>${r.chlorine.toFixed(2)}</td>
        <td>${ok ? tag('合格', 'green') : tag('超限', 'red')}</td><td class="muted">${esc(r.note || '')}</td></tr>`;
    }).join('')}`;
}

$('#rdAdd').onclick = async () => {
  try {
    await api(`/api/pools/${state.currentPool}/readings`, { method: 'POST', body: {
      level: +$('#rdLevel').value, turbidity: +$('#rdTurb').value, chlorine: +$('#rdChl').value,
      kind: $('#rdKind').value, actor: actorName(),
    }});
    toast('读数已录入' + ($('#rdKind').value === 'retest' ? '（复测，已挂到复开申请）' : ''));
    refresh();
  } catch (e) { fail(e); }
};
$('#refillBtn').onclick = () =>
  api('/api/pools/refill', { method: 'POST', body: { pool_id: state.currentPool, actor: actorName() } })
    .then(r => toast('补水完成，水位恢复')).catch(fail).then(refresh);

/* ---------------- 泵组 / 喷头 ---------------- */
function renderPumps(data) {
  const th = state.constants.thresholds;
  $('#pumpCards').innerHTML = data.pumps.map(p => {
    const hot = p.temp_c > th.pump_temp_max;
    const [txt, cls] = STATUS_TAG[p.status] || [p.status, 'gray'];
    return `<div class="card ${p.status === 'fault' || p.status === 'repair' ? 'bad' : p.status === 'running' ? 'run' : ''}">
      <div class="task-head"><h4>${esc(p.code)} ${esc(p.name)}</h4>${tag(txt, cls)}</div>
      <div class="kv"><span>所属水池</span><b>${esc(p.pool_name)}</b></div>
      <div class="kv"><span>功率</span><b>${p.power_kw} kW</b></div>
      <div class="kv"><span>泵体温度</span><b style="color:${hot ? '#C0392B' : 'inherit'}">${p.temp_c} ℃${hot ? ' 过热' : ''}</b></div>
      <div class="kv"><span>故障码</span><b>${esc(p.fault_code || '—')}</b></div>
      <div class="actions">
        <button class="btn small warn" data-hot="${p.id}">模拟过热</button>
      </div>
    </div>`;
  }).join('');
  $$('#pumpCards [data-hot]').forEach(b => b.onclick = () =>
    api('/api/sim/pump-overheat', { method: 'POST', body: { pump_id: +b.dataset.hot } })
      .then(() => toast('已注入泵体过热')).catch(fail).then(refresh));

  $('#nozzleCards').innerHTML = data.nozzle_groups.map(g => `
    <div class="card ${g.blocked || g.blocked_now ? 'bad' : ''}">
      <div class="task-head"><h4>${esc(g.code)} ${esc(g.name)}</h4>
        ${g.blocked || g.blocked_now ? tag('堵塞', 'red') : tag('正常', 'green')}</div>
      <div class="kv"><span>喷头数量</span><b>${g.nozzle_count}</b></div>
      <div class="kv"><span>当前压力</span><b style="color:${g.pressure_mpa < th.nozzle_pressure_min ? '#C0392B' : 'inherit'}">${g.pressure_mpa} MPa</b></div>
      <div class="actions">
        <button class="btn small warn" data-block="${g.id}">模拟堵塞</button>
        <button class="btn small green" data-unblock="${g.id}">疏通</button>
      </div>
    </div>`).join('');
  $$('#nozzleCards [data-block]').forEach(b => b.onclick = () =>
    api('/api/sim/nozzle-block', { method: 'POST', body: { group_id: +b.dataset.block, blocked: true } })
      .then(() => toast('已注入堵塞')).catch(fail).then(refresh));
  $$('#nozzleCards [data-unblock]').forEach(b => b.onclick = () =>
    api('/api/sim/nozzle-block', { method: 'POST', body: { group_id: +b.dataset.unblock, blocked: false } })
      .then(() => toast('压力恢复')).catch(fail).then(refresh));
}

$('#intrusionBtn').onclick = () =>
  api('/api/sim/intrusion', { method: 'POST', body: { zone_id: +$('#intrZone').value } })
    .then(r => toast(`封控区闯入，${r.pools.join('、')} 已紧急停运`)).catch(fail).then(refresh);
$('#intrusionClearBtn').onclick = () =>
  api('/api/sim/intrusion', { method: 'POST', body: { zone_id: +$('#intrZone').value, clear: true } })
    .then(() => toast('封控区已清空')).catch(fail).then(refresh);

/* ---------------- 药剂台账 ---------------- */
function renderDosing(rows) {
  const limits = state.constants.dose_limits;
  $('#doseHint').textContent = `单次上限：氯片 ${limits['氯片']}ml / 絮凝剂 ${limits['絮凝剂']}ml / 除藻剂 ${limits['除藻剂']}ml`;
  $('#dosingTable').innerHTML = `
    <tr><th>时间</th><th>水池</th><th>药剂</th><th>投加量</th><th>操作人</th><th>观察期至</th><th>备注</th></tr>
    ${rows.map(d => `<tr>
      <td>${timeShort(d.ts)}</td><td>${esc(d.pool_name)}</td><td>${esc(d.chemical)}</td>
      <td>${d.amount_ml} ml ${d.note === '超量投加' ? tag('超量', 'red') : ''}</td>
      <td>${esc(d.operator)}</td><td>${esc(d.observe_until.slice(5, 16))}</td>
      <td class="muted">${esc(d.note || '')}</td>
    </tr>`).join('')}`;
}
$('#doseAdd').onclick = async () => {
  try {
    const r = await api('/api/dosing', { method: 'POST', body: {
      pool_id: +$('#dosePool').value, chemical: $('#doseChem').value,
      amount_ml: +$('#doseAmount').value, actor: actorName(),
    }});
    toast(r.ok ? '投加完成，进入观察期' : r.message);
    refresh();
  } catch (e) { fail(e); }
};

/* ---------------- 清池流程 ---------------- */
function renderCleaning(rows, overview) {
  $('#cleaningList').innerHTML = rows.map(c => {
    const [txt, cls] = CLEAN_TAG[c.status] || [c.status, 'gray'];
    const canRetest = c.status === 'done';
    return `<div class="task ${c.status === 'done' ? 'done' : c.status === 'locked' || c.status === 'in_progress' ? 'locked' : ''}">
      <div class="task-head">
        <div class="task-title">#${c.id} ${esc(c.pool_name)}　计划日期 ${esc(c.planned_date)} ${tag(txt, cls)}</div>
        <div class="muted">养护员：${esc(c.assignee || '未指派')}</div>
      </div>
      <div class="task-body">
        ${c.started_at ? `开工 ${esc(c.started_at)}　` : ''}${c.finished_at ? `完工 ${esc(c.finished_at)}` : ''}
        ${c.note ? `　${esc(c.note)}` : ''}
      </div>
      <div class="checklist">
        <span class="${c.retest_reading_id ? 'ok' : 'no'}">${c.retest_reading_id ? '✓' : '○'} 水质复测</span>
        <span class="${c.nozzle_trial_ok === 1 ? 'ok' : 'no'}">${c.nozzle_trial_ok === 1 ? '✓' : '○'} 喷头试运行</span>
      </div>
      <div class="actions">
        ${c.status === 'planned' ? `<button class="btn small" data-c-lock="${c.id}">锁定水池并停运</button>` : ''}
        ${c.status === 'locked' ? `<button class="btn small" data-c-start="${c.id}">开始清池</button>` : ''}
        ${c.status === 'in_progress' ? `<button class="btn small green" data-c-finish="${c.id}">完工，进入复开检查</button>` : ''}
        ${canRetest ? `<button class="btn small ghost" data-c-retest="${c.pool_id}">录入复测读数</button>
          <button class="btn small ghost" data-c-trial="${c.pool_id}">喷头试运行</button>` : ''}
      </div>
    </div>`;
  }).join('') || '<p class="muted">暂无清池任务</p>';

  $$('[data-c-lock]').forEach(b => b.onclick = () => api(`/api/cleaning/${b.dataset.cLock}/lock`, { method: 'POST', body: {} })
    .then(() => toast('水池已锁定，运行计划暂停')).catch(fail).then(refresh));
  $$('[data-c-start]').forEach(b => b.onclick = () => api(`/api/cleaning/${b.dataset.cStart}/start`, { method: 'POST', body: {} })
    .then(() => toast('清池开始')).catch(fail).then(refresh));
  $$('[data-c-finish]').forEach(b => b.onclick = () => api(`/api/cleaning/${b.dataset.cFinish}/finish`, { method: 'POST', body: {} })
    .then(r => toast(`完工，复开申请 #${r.reopen_request_id} 待复测`)).catch(fail).then(refresh));
  $$('[data-c-retest]').forEach(b => b.onclick = async () => {
    const level = prompt('复测水位（0-1）', '0.78');
    if (level === null) return;
    const turbidity = prompt('复测浊度 NTU（≤5）', '2.6');
    if (turbidity === null) return;
    const chlorine = prompt('复测余氯 mg/L（0.3-1.0）', '0.55');
    if (chlorine === null) return;
    try {
      await api(`/api/pools/${b.dataset.cRetest}/readings`, { method: 'POST', body: {
        level: +level, turbidity: +turbidity, chlorine: +chlorine, kind: 'retest', actor: actorName(),
      }});
      toast('复测已录入'); refresh();
    } catch (e) { fail(e); }
  });
  $$('[data-c-trial]').forEach(b => b.onclick = () =>
    api(`/api/pools/${b.dataset.cTrial}/nozzle-trial`, { method: 'POST', body: {} })
      .then(r => toast(r.ok ? '喷头试运行全部正常' : '存在堵塞组')).catch(fail).then(refresh));
}
$('#cleanAdd').onclick = () =>
  api('/api/cleaning', { method: 'POST', body: {
    pool_id: +$('#cleanPool').value, planned_date: $('#cleanDate').value || new Date().toISOString().slice(0, 10),
    assignee: actorName(),
  }}).then(() => toast('清池任务已建立')).catch(fail).then(refresh);

/* ---------------- 故障维修 ---------------- */
function renderFaults(rows) {
  $('#faultsTable').innerHTML = `
    <tr><th>时间</th><th>水池</th><th>类型</th><th>详情</th><th>状态</th><th>处置</th><th>操作</th></tr>
    ${rows.map(f => `<tr>
      <td>${timeShort(f.ts)}</td><td>${esc(f.pool_name)}</td>
      <td>${esc(state.constants.fault_text[f.kind] || f.kind)}</td>
      <td style="max-width:340px">${esc(f.detail)}</td>
      <td>${tag(...(FAULT_TAG[f.status] || [f.status, 'gray']))}</td>
      <td class="muted">${esc(f.resolution || '—')}</td>
      <td class="actions" style="margin:0">
        ${f.status === 'open' && f.kind !== 'weather' ? `<button class="btn small" data-rep="${f.id}">开始维修</button>` : ''}
        ${f.status === 'repairing' ? `<button class="btn small green" data-repf="${f.id}">维修完工</button>` : ''}
        ${f.status === 'open' && f.kind !== 'weather' ? `<button class="btn small ghost" data-res="${f.id}">现场处置闭环</button>` : ''}
        ${f.kind === 'weather' ? '<span class="muted">预警解除后自动闭环</span>' : ''}
      </td>
    </tr>`).join('')}`;
  $$('[data-rep]').forEach(b => b.onclick = () => api(`/api/faults/${b.dataset.rep}/repair`, { method: 'POST', body: {} })
    .then(() => toast('已开始维修，水池停运')).catch(fail).then(refresh));
  $$('[data-repf]').forEach(b => b.onclick = () => api(`/api/faults/${b.dataset.repf}/finish`, { method: 'POST', body: {} })
    .then(r => toast(`维修完工，复开申请 #${r.reopen_request_id}`)).catch(fail).then(refresh));
  $$('[data-res]').forEach(b => b.onclick = () => {
    const resolution = prompt('现场处置说明', '已核查处理，现场恢复正常');
    if (!resolution) return;
    api(`/api/faults/${b.dataset.res}/resolve`, { method: 'POST', body: { resolution } })
      .then(() => toast('故障已闭环')).catch(fail).then(refresh);
  });
}

/* ---------------- 复开审批 ---------------- */
function renderReopen(rows) {
  $('#reopenList').innerHTML = rows.map(r => {
    const over = r.retest && r.within_limits === false;
    return `<div class="task ${r.status === 'approved' ? 'done' : r.status === 'rejected' ? 'locked' : ''}">
      <div class="task-head">
        <div class="task-title">#${r.id} ${esc(r.pool_name)} ${tag(...(REOPEN_TAG[r.status] || [r.status, 'gray']))}</div>
        <div class="muted">${timeShort(r.ts)}</div>
      </div>
      <div class="task-body">${esc(r.reason)}</div>
      <div class="checklist">
        <span class="${r.retest ? 'ok' : 'no'}">${r.retest ? '✓' : '○'} 水质复测
          ${r.retest ? `（水位 ${(r.retest.level * 100).toFixed(0)}% / 浊度 ${r.retest.turbidity} / 余氯 ${r.retest.chlorine}）${over ? tag('越限', 'red') : tag('合格', 'green')}` : ''}</span>
        <span class="${r.nozzle_trial_ok === 1 ? 'ok' : 'no'}">${r.nozzle_trial_ok === 1 ? '✓' : '○'} 喷头试运行</span>
      </div>
      ${r.decided_at ? `<div class="task-body">审批：${esc(r.decided_by)}（${r.decided_role === 'supervisor' ? '主管' : '养护员'}）${r.reject_reason ? '　退回原因：' + esc(r.reject_reason) : ''}</div>` : ''}
      <div class="actions">
        ${r.status === 'pending' ? `
          <button class="btn small green" data-approve="${r.id}" ${over && state.role !== 'supervisor' ? 'disabled title="越限复开仅主管可批准"' : ''}>
            批准复开${over ? '（越限，需主管）' : ''}</button>
          <button class="btn small warn" data-reject="${r.id}">退回</button>` : ''}
        ${r.status === 'rejected' ? `<button class="btn small" data-rereq="${r.pool_id}">补齐材料后重新申请</button>` : ''}
      </div>
    </div>`;
  }).join('') || '<p class="muted">暂无复开申请</p>';

  $$('[data-approve]').forEach(b => b.onclick = async () => {
    try {
      const r = await api(`/api/reopen/${b.dataset.approve}/decide`, { method: 'POST', body: { decision: 'approve', actor: actorName() } });
      toast(r.within_limits ? '已批准复开' : '越限复开已由主管批准');
    } catch (e) { fail(e); }
    refresh();
  });
  $$('[data-reject]').forEach(b => b.onclick = async () => {
    const reason = prompt('退回原因（将要求重新复测/试运行）', '复测数据异常，请重新采样');
    if (reason === null) return;
    api(`/api/reopen/${b.dataset.reject}/decide`, { method: 'POST', body: { decision: 'reject', reject_reason: reason, actor: actorName() } })
      .then(() => toast('已退回')).catch(fail).then(refresh);
  });
  $$('[data-rereq]').forEach(b => b.onclick = () =>
    api(`/api/pools/${b.dataset.rereq}/reopen-request`, { method: 'POST', body: {} })
      .then(r => toast(`已重新发起申请 #${r.id}`)).catch(fail).then(refresh));
}

/* ---------------- 模拟注入 ---------------- */
$('#simApply').onclick = () =>
  api('/api/sim/scenario', { method: 'POST', body: {
    pool_id: +$('#simPool').value, scenario: $('#simScenario').value,
  }}).then(() => toast('工况已注入，等待调度采样')).catch(fail);
let weatherBad = false;
$('#simWeatherBadge').onclick = () => {
  weatherBad = !weatherBad;
  api('/api/sim/weather', { method: 'POST', body: { bad: weatherBad } })
    .then(() => toast(weatherBad ? '已发布雷暴大风预警' : '预警已解除')).catch(fail);
};

/* ---------------- 公共 ---------------- */
function actorName() { return state.role === 'supervisor' ? '主管·王敏' : '养护员·李工'; }

function fillSelects(overview) {
  state.pools = overview.zones.flatMap(z => z.pools);
  const opts = state.pools.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  ['#simPool', '#waterPool', '#dosePool', '#cleanPool', '#schPool'].forEach(s => {
    const el = $(s);
    if (!el.options.length) el.innerHTML = opts;
  });
  $('#intrZone').innerHTML = overview.zones.map(z => `<option value="${z.id}">${esc(z.name)}</option>`).join('');
}

async function renderAlerts() {
  const rows = await api('/api/alerts');
  const bar = $('#alertBar');
  const ALERT_CN = { dosing: '药剂', retest: '复测', reopen: '复开', cleaning: '清池', reminder: '提醒', info: '通知' };
  bar.innerHTML = rows.slice(0, 6).map(a => {
    const cat = state.constants.fault_text[a.category] || ALERT_CN[a.category] || a.category;
    const pool = a.pool_name ? esc(a.pool_name) + '：' : '';
    return `<div><span class="cat">[${esc(cat)}]</span> ${pool}${esc(a.message)} <span class="muted">${timeShort(a.ts)}</span></div>`;
  }).join('');
  bar.classList.toggle('show', rows.length > 0);
}

async function refresh() {
  try {
    const [overview, schedules, logs, pumps, dosing, cleaning, faults, reopen] = await Promise.all([
      api('/api/overview'), api('/api/schedules'), api('/api/logs'),
      api('/api/pumps'), api('/api/dosing'), api('/api/cleaning'),
      api('/api/faults'), api('/api/reopen'),
    ]);
    state.zones = overview.zones;
    fillSelects(overview);
    const w = overview.weather;
    const chip = $('#weatherChip');
    chip.className = `chip weather ${w.allow_run ? 'ok' : 'bad'}`;
    chip.textContent = `天气：${w.text}（风 ${w.wind_ms}m/s）${w.allow_run ? '' : ' · 停运'}`;

    const active = $('.tabs button.active').dataset.view;
    if (active === 'overview') renderOverview(overview);
    if (active === 'calendar') renderCalendar(schedules, logs);
    if (active === 'water') await renderWater();
    if (active === 'pumps') renderPumps(pumps);
    if (active === 'dosing') renderDosing(dosing);
    if (active === 'cleaning') renderCleaning(cleaning, overview);
    if (active === 'faults') renderFaults(faults);
    if (active === 'reopen') renderReopen(reopen);
    renderAlerts();
  } catch (e) { fail(e); }
}

(async function init() {
  state.constants = await api('/api/constants');
  $('#cleanDate').value = new Date().toISOString().slice(0, 10);
  await refresh();
  state.timer = setInterval(refresh, 5000);
})();
