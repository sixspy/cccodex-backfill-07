const msg = document.getElementById('msg');
const view = document.getElementById('view');
let windows = [];
let tab = 'windows';

async function api(path, opts={}) {
  const r = await fetch(path, { headers:{'Content-Type':'application/json'}, ...opts });
  const j = await r.json().catch(()=>({}));
  if (!r.ok) {
    const err = new Error(j.error || ('HTTP '+r.status));
    err.reasonCode = j.reason_code || '';
    err.detail = j;
    throw err;
  }
  return j;
}
function setMsg(t, err=false){ msg.textContent=t||''; msg.className = err?'err':'muted'; }
function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function loadWindows(){ windows = (await api('/api/windows')).items || []; }

function stateTag(state){
  const cls = state==='已暂停' ? 'warn' : state==='已完成' ? 'ok' : '';
  return `<span class="tag ${cls}">${esc(state)}</span>`;
}

function renderWindows(){
  view.innerHTML = `<div class="wall">${windows.map(w=>`
    <article class="card">
      <h3>${esc(w.table_name)}</h3>
      <p class="muted">${esc(w.start_date)} → ${esc(w.end_date)}</p>
      <span class="tag">${esc(w.status)}</span> ${stateTag(w.run_state)}
      <p class="muted">切片 ${w.slice_count} · 已落库 ${w.loaded_count}</p>
      <p class="muted">位点 ${w.checkpoint_offset ?? '—'}${w.checkpoint_version ? ` · v${w.checkpoint_version}` : ''}${w.rejected_count ? ` · 拒签 ${w.rejected_count}` : ''}</p>
    </article>`).join('')}</div>`;
}

function winOptions(selected){
  return windows.map(w=>`<option value="${w.id}" ${String(w.id)===String(selected)?'selected':''}>#${w.id} ${esc(w.table_name)} · ${esc(w.run_state)}</option>`).join('');
}

function renderPlan(){
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">生成切片计划</button>
    <pre id="out" class="muted"></pre>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const wid = document.getElementById('wid').value;
      const j = await api(`/api/windows/${wid}/plan`, {method:'POST', body:'{}'});
      document.getElementById('out').textContent = JSON.stringify(j.slices, null, 2);
      setMsg(`已生成 ${j.slices.length} 片`);
      await loadWindows();
    }catch(e){ setMsg(e.message,true); }
  };
}

// ---- 续跑详情 + 暂停策略卡片 ----
function policyCard(state){
  const p = state.policy;
  return `<article class="card">
    <h3>暂停策略</h3>
    <p class="muted">落库路线：<b>${esc(p.route_name)}</b></p>
    <p class="muted">未选路线：${esc(p.route_alternative)}</p>
    <p class="muted">现行偏移号：<span class="tag accent">${p.offset_no}</span>
      ${p.finished ? '' : `→ 下一片 #${p.next_slice_id} ${esc(p.next_slice_date)}`}</p>
    <ul class="policy">${p.lines.map(l=>`<li>${esc(l)}</li>`).join('')}</ul>
    <p class="muted small">${esc(p.route_note)}</p>
  </article>`;
}

function resumeDetailCard(state){
  const r = state.resume;
  const last = r.last_checkpoint;
  return `<article class="card">
    <h3>续跑详情</h3>
    <p>状态 ${stateTag(state.state)}</p>
    <p class="muted">现行偏移号 <span class="tag accent">${r.expected_offset}</span> · 下一位点版本 v${r.expected_version}</p>
    <p class="muted">下一片：${r.finished ? '无（已走完）' : `#${r.next_slice_id} ${esc(r.next_slice_date)}`}</p>
    <p class="muted">最近有效位点：${last ? `v${last.version} · 偏移 ${last.offset_no} · ${esc(last.created_at)}` : '暂无'}</p>
    <p class="muted">暂停于：${esc(state.paused_at || '—')}</p>
    <div class="row">
      <button id="btn-pause" type="button">暂停</button>
      <button id="btn-resume" class="primary" type="button" ${r.finished?'disabled':''}>按现行偏移续灌</button>
    </div>
    <label>或手工提交位点（旧位点会被拒签）
      <input id="cp-offset" type="number" min="1" placeholder="偏移号，如 ${r.expected_offset}" />
    </label>
    <button id="btn-submit" type="button">提交手工位点</button>
  </article>`;
}

function checkpointsCard(state){
  const items = state.checkpoints || [];
  return `<article class="card wide">
    <h3>位点列表 <span class="muted small">有效 / 拒签均在账册可核</span></h3>
    ${items.length ? `<table class="grid">
      <thead><tr><th>#</th><th>版本</th><th>提交偏移</th><th>现行偏移</th><th>判定</th><th>原因</th><th>来源</th><th>时间</th></tr></thead>
      <tbody>${items.map(c=>`<tr class="${c.decision==='拒签'?'bad':''}">
        <td>${c.id}</td><td>v${c.version}</td><td>${c.offset_no}</td><td>${c.current_offset}</td>
        <td><span class="tag ${c.decision==='拒签'?'bad':'ok'}">${esc(c.decision)}</span></td>
        <td>${esc(c.reason)}${c.reason_code && c.reason_code!=='OK'?` <span class="muted small">${esc(c.reason_code)}</span>`:''}</td>
        <td>${esc(c.source)}</td><td class="muted small">${esc(c.created_at)}</td>
      </tr>`).join('')}</tbody></table>`
      : '<p class="muted">尚无位点记录</p>'}
  </article>`;
}

function directoryCard(state){
  const dir = state.directory || [];
  return `<article class="card wide">
    <h3>回灌目录 <span class="muted small">偏移号与目录须同亮新位点</span></h3>
    ${dir.length ? `<table class="grid">
      <thead><tr><th>偏移</th><th>切片日</th><th>状态</th><th>行数</th></tr></thead>
      <tbody>${dir.map(d=>`<tr class="${d.is_next?'next':''}">
        <td>${d.ordinal}${d.is_next?' <span class="tag accent">下一片</span>':''}</td>
        <td>${esc(d.slice_date)}</td>
        <td><span class="tag ${d.is_loaded?'ok':''}">${esc(d.status)}</span></td>
        <td>${d.row_count}</td>
      </tr>`).join('')}</tbody></table>`
      : '<p class="muted">先生成切片计划</p>'}
    ${state.coverage.gaps.length ? `<p class="err small">检测到空档：${state.coverage.gaps.map(g=>`第${g.ordinal}片 ${esc(g.slice_date)}`).join('、')}</p>` : ''}
  </article>`;
}

async function renderResume(wid){
  if (!wid) { view.innerHTML = '<p class="muted">暂无回灌窗口</p>'; return; }
  const state = await api(`/api/windows/${wid}/resume-state`);
  view.innerHTML = `<div class="card">
      <label>窗口<select id="wid">${winOptions(wid)}</select></label>
    </div>
    <div class="wall">
      ${resumeDetailCard(state)}
      ${policyCard(state)}
    </div>
    <div class="wall">
      ${checkpointsCard(state)}
      ${directoryCard(state)}
    </div>`;
  document.getElementById('wid').onchange = (e)=> renderResume(e.target.value);

  document.getElementById('btn-pause').onclick = async ()=>{
    try{
      await api(`/api/windows/${wid}/pause`, {method:'POST', body:'{}'});
      setMsg('已暂停，续灌位点已锁在现行偏移');
      await renderResume(wid); await loadWindows();
    }catch(e){ setMsg(e.message,true); }
  };
  document.getElementById('btn-resume').onclick = async ()=>{
    try{
      const j = await api(`/api/windows/${wid}/resume`, {method:'POST',
        body: JSON.stringify({version: state.resume.expected_version, offset_no: state.resume.expected_offset})});
      setMsg(`续灌落位点 v${j.state.resume.last_checkpoint.version} · 偏移 ${j.state.resume.last_checkpoint.offset_no}`);
      await renderResume(wid); await loadWindows();
    }catch(e){ setMsg(rejectMsg(e), true); }
  };
  document.getElementById('btn-submit').onclick = async ()=>{
    const raw = document.getElementById('cp-offset').value;
    try{
      const j = await api(`/api/windows/${wid}/resume`, {method:'POST',
        body: JSON.stringify({version: state.resume.expected_version, offset_no: Number(raw)})});
      setMsg(`手工位点已落：偏移 ${j.state.resume.last_checkpoint.offset_no}`);
      await renderResume(wid); await loadWindows();
    }catch(e){ setMsg(rejectMsg(e), true); await renderResume(wid); }
  };
}

function rejectMsg(e){
  if (e.reasonCode) return `拒签（${e.reasonCode}）：${e.message}；现行偏移 ${e.detail.current_offset}`;
  return e.message;
}

async function renderReplay(){
  await loadWindows();
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <div id="list" class="wall" style="margin-top:0.8rem"></div>
  </div>`;
  const draw = async ()=>{
    const id = document.getElementById('wid').value;
    const items = (await api(`/api/windows/${id}/slices`)).items || [];
    document.getElementById('list').innerHTML = items.map(s=>`
      <article class="card">
        <h3>${esc(s.slice_date)}</h3>
        <span class="tag">${esc(s.status)}</span>
        <p class="muted">行数 ${s.row_count}</p>
        <button data-id="${s.id}" class="primary" type="button" ${s.status!=='待重放'?'disabled':''}>重放</button>
      </article>`).join('') || '<p class="muted">先生成切片计划</p>';
    document.querySelectorAll('#list button[data-id]').forEach(btn=>{
      btn.onclick = async ()=>{
        try{
          const j = await api(`/api/slices/${btn.dataset.id}/replay`, {method:'POST', body:'{}'});
          setMsg(`已落库 ${j.slice.slice_date} · ${j.slice.row_count} 行`);
          await draw(); await loadWindows();
        }catch(e){ setMsg(rejectMsg(e),true); }
      };
    });
  };
  document.getElementById('wid').onchange = draw;
  await draw();
}

async function renderCoverage(){
  const items = (await api('/api/coverage-reports')).items || [];
  view.innerHTML = `<div class="card">
    <label>窗口<select id="wid">${winOptions()}</select></label>
    <button class="primary" id="go" type="button">跑覆盖对账</button>
    <div class="wall" style="margin-top:0.8rem">${items.map(r=>`
      <article class="card"><h3>窗口 #${r.window_id}</h3><p>${esc(r.summary)}</p></article>`).join('')}</div>
  </div>`;
  document.getElementById('go').onclick = async ()=>{
    try{
      const j = await api('/api/coverage-reports', {method:'POST', body: JSON.stringify({window_id: Number(document.getElementById('wid').value)})});
      setMsg(j.report.summary);
      await renderCoverage();
    }catch(e){ setMsg(rejectMsg(e),true); }
  };
}

async function show(t){
  tab = t; setMsg(''); await loadWindows();
  if (tab==='windows') renderWindows();
  if (tab==='plan') renderPlan();
  if (tab==='replay') await renderReplay();
  if (tab==='resume') await renderResume(windows[0]?.id);
  if (tab==='coverage') await renderCoverage();
}
document.querySelectorAll('.chips button').forEach(btn=>{
  btn.onclick=()=>{ document.querySelectorAll('.chips button').forEach(b=>b.classList.remove('on')); btn.classList.add('on'); show(btn.dataset.tab); };
});
document.getElementById('btn-logout').onclick = async ()=>{ await api('/api/logout',{method:'POST',body:'{}'}); location.href='/login'; };
show('windows');
