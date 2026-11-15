// 思谛 STDeel 管理台脚本（2026-11-14：外置以支持 CSP script-src 'self'，
// 并移除内联 onclick 拼接用户名导致的存储型 XSS）
const $ = s => document.querySelector(s);
let all = [];

// 统一请求封装：会话过期（401）或密码未配置（503）时直接回登录页，避免页面显示半截数据
async function api(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401 || res.status === 503) {
    location.href = '/webui/login?next=' + encodeURIComponent(location.pathname);
    throw new Error('需要登录');
  }
  return res;
}

async function logout() {
  try { await fetch('/webui/logout', { method: 'POST' }); } catch (e) {}
  location.href = '/webui/login';
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function enc(s) { return encodeURIComponent(String(s ?? '')); }

async function loadUsers() {
  const res = await api('/users?page=1&page_size=100');
  const data = await res.json();
  all = data.items || [];
  $('#totalLabel').textContent = `用户数：${data.total}`;
  renderUsers($('#search').value);
}

function renderUsers(q = '') {
  const kw = q.trim().toLowerCase();
  const rows = all
    .filter(u => !kw || (u.username||'').toLowerCase().includes(kw)
              || String(u.user_id||'').toLowerCase().includes(kw)
              || String(u.device_id||'').toLowerCase().includes(kw))
    .map(u => `
      <tr class="clickable" data-user-id="${esc(u.user_id)}">
        <td class="mono">${esc(u.user_id)}</td>
        <td>${esc(u.username || '—')}</td>
        <td class="meta">${esc(u.device_id || '—')}</td>
        <td class="meta">${esc((u.created_at||'').replace('T',' ').slice(0,16))}</td>
        <td><button class="btn danger sm" data-action="delete" data-user-id="${esc(u.user_id)}" data-username="${esc(u.username || u.user_id)}">删除</button></td>
      </tr>`).join('');
  $('#userBody').innerHTML = rows || '<tr><td colspan="5" class="empty">无匹配用户</td></tr>';
}

async function selectUser(userId, el) {
  document.querySelectorAll('#userBody tr.active').forEach(t => t.classList.remove('active'));
  if (el) el.classList.add('active');
  const u = all.find(x => x.user_id === userId);
  $('#detailTitle').innerHTML = `📊 详情：<span class="tag">${esc(u ? (u.username || u.user_id) : userId)}</span>`;
  loadApiKeys(userId);
  loadMastery(userId);
  loadRecords(userId);
  loadClientTokenStatus(userId);
}

async function loadClientTokenStatus(userId) {
  const box = $('#clientTokenBox');
  box.textContent = '加载中…';
  try {
    const res = await api(`/users/client-token-status?user_id=${enc(userId)}`);
    const data = await res.json();
    renderClientTokenStatus(box, userId, data);
  } catch (e) {
    box.textContent = '加载失败：' + e.message;
  }
}

function renderClientTokenStatus(box, userId, data) {
  box.innerHTML = '';
  const line = document.createElement('div');
  const state = document.createElement('span');
  state.className = 'meta';
  state.textContent = data.configured
    ? '已配置（更新于 ' + String(data.updated_at || '').slice(0, 19).replace('T', ' ') + '）'
    : '未配置：App 调用 /users/api-key 会返回 403，需要先生成令牌';
  const btn = document.createElement('button');
  btn.className = 'btn';
  btn.textContent = data.configured ? '轮换令牌' : '生成令牌';
  btn.addEventListener('click', () => rotateClientToken(userId));
  line.appendChild(state);
  line.appendChild(document.createTextNode(' '));
  line.appendChild(btn);
  box.appendChild(line);
  const hint = document.createElement('div');
  hint.className = 'meta';
  hint.style.marginTop = '6px';
  hint.textContent = 'App 请求 /users/api-key 时需带请求头 X-STDeel-Token（或 ?client_token=）。令牌明文只在生成时显示一次。';
  box.appendChild(hint);
}

async function rotateClientToken(userId) {
  if (!confirm('生成/轮换后旧令牌立即失效，App 需要更新为新令牌。继续？')) return;
  const box = $('#clientTokenBox');
  try {
    const res = await api(`/users/client-token?user_id=${enc(userId)}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.status);
    box.innerHTML = '';
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = '新令牌（只显示这一次，请立即复制到 App）：';
    const pre = document.createElement('pre');
    pre.className = 'mono';
    pre.style.whiteSpace = 'pre-wrap';
    pre.style.wordBreak = 'break-all';
    pre.style.marginTop = '6px';
    pre.textContent = data.client_token;
    const copy = document.createElement('button');
    copy.className = 'btn sm';
    copy.textContent = '复制';
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(data.client_token);
        copy.textContent = '已复制';
      } catch (e) {
        copy.textContent = '请手动选择复制';
      }
    });
    box.appendChild(meta);
    box.appendChild(pre);
    box.appendChild(copy);
  } catch (e) {
    box.textContent = '生成失败：' + e.message;
  }
}

async function loadApiKeys(userId) {
  const box = $('#apiKeyBox');
  box.innerHTML = '<span class="empty">加载中…</span>';
  try {
    // 管理台只拿掩码：服务端返回前 3 / 后 3 位，完整 Key 只在密文里，不下发
    const res = await api(`/users/api-key-masked?user_id=${enc(userId)}`);
    const keys = await res.json();
    if (!keys || !keys.length) { box.innerHTML = '<span class="empty">该用户暂无 API Key</span>'; return; }
    box.innerHTML = keys.map(k => `
      <div style="padding:8px 0;border-bottom:1px solid #26324a">
        <div><span class="pill ${k.enabled?'on':'off'}">${k.enabled?'启用':'停用'}</span>
        <span class="meta">${esc(k.name || '未命名')}</span></div>
        <div class="mono" style="margin-top:4px">${esc(k.masked)}</div>
      </div>`).join('')
      + '<div class="meta" style="margin-top:8px">为保护 Key，这里只显示前 3 位和后 3 位；完整值仅客户端换机同步时可取。</div>';
  } catch(e) {
    box.innerHTML = `<span class="empty">加载失败：${esc(e.message)}</span>`;
  }
}

function pct(c, w) { return c + w === 0 ? 0 : Math.round(w / (c + w) * 100); }

async function loadMastery(userId) {
  const box = $('#masteryBox');
  box.innerHTML = '<span class="empty">加载中…</span>';
  try {
    const res = await api(`/users/${enc(userId)}/mastery`);
    const data = await res.json();
    const items = data.items || [];
    if (!items.length) { box.innerHTML = '<span class="empty">暂无知识点掌握数据</span>'; return; }
    box.innerHTML = items.slice(0, 40).map(m => {
      const rate = pct(m.correct_count, m.wrong_count);
      return `
      <div style="padding:8px 0;border-bottom:1px solid #26324a">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
          <span>${esc(m.knowledge_point)} <span class="meta">${esc(m.subject || '')}</span></span>
          <span class="meta">✅${m.correct_count} ❌${m.wrong_count} <span class="rate">${rate}%</span></span>
        </div>
        <div class="bar"><i style="width:${Math.max(3, rate)}%"></i></div>
      </div>`;
    }).join('');
  } catch(e) {
    box.innerHTML = `<span class="empty">加载失败：${esc(e.message)}</span>`;
  }
}

function fbLabel(f) {
  if (f === 'correct') return '<span class="fb-correct">✓ 正确</span>';
  if (f === 'partial') return '<span class="fb-partial">◐ 部分错误</span>';
  if (f === 'wrong') return '<span class="fb-wrong">✗ 错误</span>';
  return '<span class="fb-none">未标注</span>';
}
function actionLabel(a) {
  const map = { solve: '解题', retry: '重答', detail: '疑问', correct: '正确', partial: '部分错误', wrong: '错误' };
  return map[a] || a || '—';
}

async function loadRecords(userId) {
  const box = $('#recordsBox');
  box.innerHTML = '<span class="empty">加载中…</span>';
  try {
    const res = await api(`/users/${enc(userId)}/records?limit=100`);
    const records = await res.json();
    if (!Array.isArray(records) || !records.length) { box.innerHTML = '<span class="empty">该用户暂无解题记录</span>'; return; }
    box.innerHTML = records.map(r => {
      const kps = Array.isArray(r.knowledge_points) ? r.knowledge_points.join('、') : (r.knowledge_points || '—');
      return `
      <div class="record">
        <div class="q">${esc(r.question_text || '（空题干）')}</div>
        <div class="a">${r.answer ? '答案：' + esc(r.answer) : '无答案'}</div>
        <div class="meta-row">
          <span>${fbLabel(r.user_feedback)}</span>
          <span class="meta">动作：${esc(actionLabel(r.action_type))}</span>
          <span>${esc(r.subject || '未分类')}</span>
          <span class="mono">${esc(r.ai_model || '—')}</span>
          <span>${esc(r.latency_ms ?? '')}ms</span>
          <span>${esc(kps || '—')}</span>
          <span>#${esc(r.id)}</span>
          <span>${esc((r.created_at||'').replace('T',' ').slice(0,16))}</span>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    box.innerHTML = `<span class="empty">加载失败：${esc(e.message)}</span>`;
  }
}

async function deleteUser(userId, username) {
  const ok = confirm(`确定删除用户「${username}」(${userId})？\n将一并删除其解题记录、知识点掌握、API Key，且不可恢复！`);
  if (!ok) return;
  try {
    const res = await api(`/users/${enc(userId)}`, { method: 'DELETE' });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      alert('删除失败：' + (body.detail || res.status));
      return;
    }
    alert('已删除用户 ' + username);
    refreshAll();
  } catch(e) {
    alert('删除失败：' + e.message);
  }
}

async function refreshAll() {
  await loadUsers();
}

$('#search').addEventListener('input', e => renderUsers(e.target.value));
$('#btnRefresh').addEventListener('click', () => refreshAll());
$('#btnLogout').addEventListener('click', () => logout());

// 事件委托：行内不再拼 onclick，用户名/ID 只作为 data-* 文本传递
$('#userBody').addEventListener('click', event => {
  const del = event.target.closest('[data-action="delete"]');
  if (del) {
    event.stopPropagation();
    deleteUser(del.dataset.userId, del.dataset.username);
    return;
  }
  const row = event.target.closest('tr[data-user-id]');
  if (row) selectUser(row.dataset.userId, row);
});

loadUsers();
