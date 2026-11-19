// 代理客户端的日常控制与故障保护的处置顺序：
// 订阅（只读）、当前节点、系统代理 / TUN 独立开关、处置链的顺序与启停。
// 接口契约见 docs/proxy-control-spec.md 第 4 节。

const STEPS = {
  switch_node: { title: '切换节点', desc: '在当前代理组换到测速最快的可用节点，成功即停止后续步骤', phrase: '换节点' },
  close_tun: { title: '关闭 TUN', desc: '停止接管全部流量，程序改走 WiFi 直连', phrase: '关闭 TUN' },
  close_proxy: { title: '关闭系统代理', desc: '撤下系统代理，浏览器等改走直连', phrase: '关闭系统代理' }
};
const STEP_IDS = Object.keys(STEPS);
const DEFAULT_FALLBACK = {
  order: ['switch_node', 'close_tun', 'close_proxy'],
  enabled: { switch_node: false, close_tun: true, close_proxy: true }
};

const SWITCHES = {
  proxy: { label: '系统代理', hint: '浏览器等读取系统代理的程序走代理' },
  tun: { label: 'TUN', hint: '接管全部流量，包括不读系统代理的程序' }
};

const RESULTS = {
  done: { text: '已完成', path: 'm6 12 4 4 8-8' },
  skipped: { text: '已跳过', path: 'M7 12h10' },
  failed: { text: '未成功', path: 'm7 7 10 10M17 7 7 17' }
};

const SUBSCRIPTION_TTL = 60_000;

// 拼接界面文字：中文与英文、数字相邻处补一个空格，例如「已开启 TUN」
const isCjk = ch => /[㐀-鿿]/.test(ch);
const isLatin = ch => /[A-Za-z0-9]/.test(ch);
function mix(...parts) {
  return parts.filter(Boolean).reduce((text, part) => {
    if (!text) return part;
    const a = text.at(-1);
    const b = part[0];
    return text + ((isCjk(a) && isLatin(b)) || (isLatin(a) && isCjk(b)) ? ' ' : '') + part;
  }, '');
}

function svg(path, extra = '') {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extra}><path d="${path}"/></svg>`;
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  const gb = bytes / 1024 ** 3;
  if (gb >= 1024) return `${(gb / 1024).toFixed(gb >= 10240 ? 0 : 1)} TB`;
  if (gb >= 1) return `${gb.toFixed(gb >= 100 ? 0 : 1)} GB`;
  return `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`;
}

function parseDate(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatExpire(iso, now = new Date()) {
  const date = parseDate(iso);
  if (!date) return { text: '', warn: false };
  const days = Math.ceil((date - now) / 86_400_000);
  if (days < 0) return { text: '已过期', warn: true };
  if (days <= 3) return { text: days === 0 ? '今天到期' : `${days} 天后到期`, warn: true };
  const sameYear = date.getFullYear() === now.getFullYear();
  return { text: `${sameYear ? '' : `${date.getFullYear()} 年 `}${date.getMonth() + 1} 月 ${date.getDate()} 日到期`, warn: false };
}

function formatUpdated(iso, now = Date.now()) {
  const date = parseDate(iso);
  if (!date) return '';
  const minutes = Math.round((now - date.getTime()) / 60_000);
  if (minutes < 1) return '刚刚更新';
  if (minutes < 60) return `${minutes} 分钟前更新`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} 小时前更新`;
  return `${Math.round(hours / 24)} 天前更新`;
}

function formatMoment(iso) {
  const date = parseDate(iso);
  if (!date) return '';
  const pad = n => String(n).padStart(2, '0');
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return date.toDateString() === new Date().toDateString() ? time : `${date.getMonth() + 1}-${pad(date.getDate())} ${time}`;
}

function validFallback(raw) {
  if (!raw || !Array.isArray(raw.order) || !raw.enabled) return null;
  const order = raw.order.filter(step => STEP_IDS.includes(step));
  if (order.length !== STEP_IDS.length || new Set(order).size !== order.length) return null;
  const enabled = {};
  for (const step of STEP_IDS) enabled[step] = raw.enabled[step] === true;
  return { order, enabled };
}

function sameFallback(a, b) {
  return Boolean(a && b) && a.order.join() === b.order.join() && STEP_IDS.every(step => a.enabled[step] === b.enabled[step]);
}

/** 处置顺序的白话解释：按当前顺序和启停状态拼出一句话。 */
function describeOrder(config, autoRestore) {
  const steps = config.order.filter(step => config.enabled[step]);
  if (!steps.length) return '三个步骤都已停用：出口异常时只提示，不做处置。';
  if (steps.length === 1) {
    return steps[0] === 'switch_node' ? '只尝试换节点；换不通时保持现状。' : `只${STEPS[steps[0]].phrase}，不更换节点。`;
  }
  const clauses = steps.map((step, index) => {
    if (index === 0) return `先${STEPS[step].phrase}`;
    return (steps[index - 1] === 'switch_node' ? '换不通再' : '再') + STEPS[step].phrase;
  });
  const switchAt = steps.indexOf('switch_node');
  let tail = '不更换节点。';
  if (switchAt === 0) tail = '尽量不断代理，但测速的几秒内仍走失效出口。';
  else if (switchAt > 0) tail = autoRestore ? '先保住联网；新节点稳定后自动恢复已关闭的开关。' : '先保住联网；新节点可用后，需手动恢复已关闭的开关。';
  return `${clauses.join('，')}。${tail}`;
}

export function initProxyControls({ apiRequest, showToast, getClient, onChanged }) {
  const post = (path, payload) =>
    apiRequest(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  const panel = document.getElementById('panel-probes');
  const visible = () => !panel || !panel.hidden;
  const reducedMotion = () => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── 订阅（只读） ─────────────────────────────────────────
  const sub = document.getElementById('proxy-sub');
  const subName = document.getElementById('proxy-sub-name');
  const subMeter = document.getElementById('proxy-sub-meter');
  const subMeta = document.getElementById('proxy-sub-meta');
  let subFetchedAt = 0;
  let subEpoch = 0;
  let subPending = null;

  function renderSubscription(data) {
    subMeter.hidden = true;
    sub.dataset.warn = 'false';
    if (!data || !data.available) {
      sub.dataset.state = 'empty';
      subName.textContent = '—';
      subName.removeAttribute('title');
      subMeta.textContent = data?.message || '未读取到订阅';
      return;
    }
    sub.dataset.state = 'ready';
    const name = data.name || '未命名订阅';
    subName.textContent = name;
    subName.title = name;
    const parts = [];
    let warn = false;
    if (data.kind === 'local') parts.push('本地配置');
    const usage = data.usage;
    if (usage && Number.isFinite(usage.used)) {
      if (usage.total > 0) {
        const ratio = Math.min(1, Math.max(0, usage.used / usage.total));
        subMeter.value = ratio;
        subMeter.hidden = false;
        subMeter.setAttribute('aria-valuetext', `已用 ${Math.round(ratio * 100)}%`);
        subMeter.title = `已用 ${Math.round(ratio * 100)}%`;
        parts.push(`已用 ${formatBytes(usage.used)} / ${formatBytes(usage.total)}`);
        warn = ratio >= 0.9;
      } else {
        parts.push(`已用 ${formatBytes(usage.used)} · 不限流量`);
      }
      const expire = formatExpire(usage.expire);
      if (expire.text) parts.push(expire.text);
      warn = warn || expire.warn;
    }
    const updated = formatUpdated(data.updated_at);
    if (updated) parts.push(updated);
    subMeta.textContent = parts.join(' · ');
    sub.dataset.warn = String(warn);
  }

  function refreshSubscription(force = false) {
    if (!force && (subPending || Date.now() - subFetchedAt < SUBSCRIPTION_TTL)) return subPending;
    const epoch = ++subEpoch;
    const client = getClient();
    subPending = apiRequest('/api/proxy/subscription')
      .then(data => {
        if (epoch !== subEpoch || (data.client && data.client !== client)) return;
        subFetchedAt = Date.now();
        renderSubscription(data);
      })
      .catch(error => {
        if (epoch === subEpoch) renderSubscription({ available: false, message: error.message || '订阅读取失败' });
      })
      .finally(() => { if (epoch === subEpoch) subPending = null; });
    return subPending;
  }

  // ── 当前节点 ─────────────────────────────────────────────
  const nodeCurrent = document.getElementById('proxy-node-current');

  function renderNode(status) {
    const chain = typeof status.egress === 'string' ? status.egress : '';
    const delay = Number.isFinite(status.exit_delay) && status.exit_delay > 0 ? `${Math.round(status.exit_delay)} ms` : '';
    let text = '';
    if (chain) text = delay ? `${chain} · ${delay}` : chain;
    else if (status.connected === false) text = '未连接客户端';
    else text = '出口未确认，可点「检测出口」';
    nodeCurrent.textContent = text;
    nodeCurrent.title = chain;
    nodeCurrent.dataset.state = status.exit_state === 'failed' ? 'failed' : chain ? 'ready' : 'empty';
  }

  // ── 系统代理 / TUN 独立开关 ───────────────────────────────
  const switches = {};
  for (const field of Object.keys(SWITCHES)) {
    switches[field] = {
      input: document.getElementById(`proxy-sw-${field}`),
      chip: document.getElementById(`proxy-sw-${field}-chip`),
      hint: document.getElementById(`proxy-sw-${field}-hint`),
      row: document.querySelector(`.proxy-switch-row[data-field="${field}"]`)
    };
  }
  let switchData = null;
  let switchBusy = null; // 正在切换的字段，同一时间只操作一个开关
  let switchEpoch = 0;
  let switchPending = null;
  let autoRestore = false;

  function setChip(chip, text, tone = 'muted') {
    chip.hidden = !text;
    chip.textContent = text || '';
    chip.className = `chip chip-${tone}`;
  }

  function renderSwitches() {
    const data = switchData;
    for (const [field, meta] of Object.entries(SWITCHES)) {
      const { input, chip, hint, row } = switches[field];
      if (switchBusy === field) continue;
      const value = data?.[field];
      const known = typeof value === 'boolean';
      const guarded = Array.isArray(data?.guarded) && data.guarded.includes(field);
      input.checked = known && value;
      input.disabled = !known || Boolean(data?.restoring) || switchBusy !== null;
      row.dataset.state = guarded ? 'guarded' : known ? (value ? 'on' : 'off') : 'unknown';
      let hintText = meta.hint;
      if (!data) {
        setChip(chip, '');
        hintText = '连接客户端后显示开关状态';
      } else if (data.restoring) {
        setChip(chip, '恢复中', 'busy');
        hintText = '正在恢复被故障保护关闭的开关，请稍候';
      } else if (guarded) {
        setChip(chip, '保护已关闭', 'busy');
        hintText = autoRestore ? '出口稳定后自动恢复；也可以现在手动开启' : '可点「恢复代理」，或在这里手动开启';
      } else if (!known) {
        setChip(chip, '');
        hintText = '未读取到客户端状态，请先连接客户端';
      } else if (field === 'proxy' && data.proxy_foreign) {
        setChip(chip, '其他程序', 'muted');
        hintText = '系统代理当前由其他程序设置；开启会改为本客户端';
      } else {
        setChip(chip, '');
      }
      hint.textContent = hintText;
    }
  }

  function refreshSwitches() {
    if (switchPending) return switchPending;
    const epoch = ++switchEpoch;
    const client = getClient();
    switchPending = apiRequest('/api/proxy/switches')
      .then(data => {
        if (epoch !== switchEpoch || switchBusy || (data.client && data.client !== client)) return;
        switchData = data;
        renderSwitches();
      })
      .catch(() => {
        if (epoch !== switchEpoch || switchBusy) return;
        switchData = { proxy: null, tun: null, guarded: [] };
        renderSwitches();
      })
      .finally(() => { if (epoch === switchEpoch) switchPending = null; });
    return switchPending;
  }

  for (const [field, meta] of Object.entries(SWITCHES)) {
    const { input, chip, row } = switches[field];
    input.addEventListener('change', async () => {
      if (switchBusy) { input.checked = !input.checked; return; }
      const desired = input.checked;
      const client = getClient();
      switchBusy = field;
      switchEpoch += 1; // 作废进行中的读取，避免旧状态覆盖
      switchPending = null;
      for (const other of Object.values(switches)) other.input.disabled = true;
      setChip(chip, desired ? '正在开启…' : '正在关闭…', 'muted');
      row.setAttribute('aria-busy', 'true');
      try {
        const data = await post('/api/proxy/switch', { client, field, enabled: desired });
        if (client === getClient()) switchData = data;
        if (data.warning) showToast(mix('已开启', meta.label, '。', data.warning), 'warning', 6000);
        else showToast(mix('已', desired ? '开启' : '关闭', meta.label), 'success');
      } catch (error) {
        input.checked = !desired;
        showToast(error.message || mix(meta.label, '切换未完成'), 'error', 5000);
      } finally {
        switchBusy = null;
        row.removeAttribute('aria-busy');
        renderSwitches();
        refreshSwitches();
        onChanged?.();
      }
    });
  }

  // ── 处置顺序 ─────────────────────────────────────────────
  const chainBox = document.getElementById('proxy-fallback');
  const chainList = document.getElementById('proxy-fallback-list');
  const chainHint = document.getElementById('proxy-fallback-hint');
  const chainSaving = document.getElementById('proxy-fallback-saving');
  const lastBox = document.getElementById('proxy-fallback-last');
  const lastTime = document.getElementById('proxy-fallback-last-time');
  const lastSteps = document.getElementById('proxy-fallback-last-steps');
  const items = new Map();
  let confirmed = null; // 服务端确认过的设置
  let draft = null; // 尚未保存完成的本地设置
  let saving = false;
  let savedTimer = null;
  let nodePaused = false;
  let guardActive = false;

  function createItem(step) {
    const meta = STEPS[step];
    const li = document.createElement('li');
    li.className = 'chain-step';
    li.dataset.step = step;
    const titleId = `proxy-fallback-${step}-title`;
    li.innerHTML = `
      <span class="chain-index" aria-hidden="true"></span>
      <span class="chain-text">
        <span class="chain-title-line"><span class="chain-title" id="${titleId}"></span><span class="chip chip-busy chain-paused" title="30 分钟内已自动换过 3 次节点，暂停自动换节点；手动选一次节点即可解除" hidden>已暂停</span></span>
        <span class="chain-desc"></span>
      </span>
      <span class="chain-move">
        <button type="button" class="icon-btn icon-btn-xs" data-move="-1">${svg('m6 15 6-6 6 6')}</button>
        <button type="button" class="icon-btn icon-btn-xs" data-move="1">${svg('m6 9 6 6 6-6')}</button>
      </span>
      <input type="checkbox" class="switch">`;
    li.querySelector('.chain-title').textContent = meta.title;
    li.querySelector('.chain-desc').textContent = meta.desc;
    const [up, down] = li.querySelectorAll('[data-move]');
    up.setAttribute('aria-label', `上移「${meta.title}」`);
    down.setAttribute('aria-label', `下移「${meta.title}」`);
    const toggle = li.querySelector('.switch');
    toggle.setAttribute('aria-labelledby', titleId);
    toggle.addEventListener('change', () => {
      const next = structuredClone(draft || confirmed || DEFAULT_FALLBACK);
      next.enabled[step] = toggle.checked;
      commit(next, `已${toggle.checked ? '启用' : '停用'}「${meta.title}」`);
    });
    for (const button of [up, down]) {
      button.addEventListener('click', () => move(step, Number(button.dataset.move)));
    }
    items.set(step, li);
    return li;
  }

  function move(step, offset) {
    const current = draft || confirmed || DEFAULT_FALLBACK;
    const from = current.order.indexOf(step);
    const to = from + offset;
    if (from < 0 || to < 0 || to >= current.order.length) return;
    const next = structuredClone(current);
    [next.order[from], next.order[to]] = [next.order[to], next.order[from]];
    commit(next, '处置顺序已保存');
  }

  function renderChain() {
    const config = draft || confirmed || DEFAULT_FALLBACK;
    const focused = chainList.contains(document.activeElement) ? document.activeElement : null;
    const before = new Map([...items].map(([step, li]) => [step, li.getBoundingClientRect().top]));
    let position = 0;
    config.order.forEach((step, index) => {
      const li = items.get(step) || createItem(step);
      const enabled = config.enabled[step];
      li.dataset.enabled = String(enabled);
      li.querySelector('.chain-index').textContent = enabled ? String(++position) : '–';
      li.querySelector('.switch').checked = enabled;
      const [up, down] = li.querySelectorAll('[data-move]');
      up.disabled = index === 0;
      down.disabled = index === config.order.length - 1;
      const paused = li.querySelector('.chain-paused');
      if (paused) paused.hidden = !(step === 'switch_node' && nodePaused && enabled);
      if (chainList.children[index] !== li) chainList.insertBefore(li, chainList.children[index] || null);
    });
    // 重新插入会让焦点掉到页面上：还给原按钮；按钮到端点后不可用时，交给同一行的另一个方向
    if (focused && document.activeElement !== focused) {
      const target = focused.disabled ? focused.closest('.chain-step')?.querySelector('[data-move]:not(:disabled)') : focused;
      target?.focus({ preventScroll: true });
    }
    if (!reducedMotion()) {
      for (const [step, li] of items) {
        const delta = (before.get(step) ?? 0) - li.getBoundingClientRect().top;
        if (before.has(step) && Math.abs(delta) > 1) {
          li.animate([{ transform: `translateY(${delta}px)` }, { transform: 'none' }], { duration: 240, easing: 'cubic-bezier(0.2, 0.7, 0.2, 1)' });
        }
      }
    }
    chainBox.dataset.active = String(guardActive);
    const sentence = describeOrder(config, autoRestore);
    chainHint.textContent = guardActive ? sentence : `自动处置未开启，开启后按此顺序执行。${sentence}`;
  }

  function setSaving(text) {
    clearTimeout(savedTimer);
    chainSaving.textContent = text;
    if (text === '已保存') savedTimer = setTimeout(() => { chainSaving.textContent = ''; }, 1800);
  }

  let pendingMessage = '';

  async function commit(next, message) {
    draft = next;
    pendingMessage = message;
    renderChain();
    if (saving) return; // 进行中的保存结束后会带上最新的草稿
    saving = true;
    setSaving('正在保存…');
    try {
      while (draft && !sameFallback(draft, confirmed)) {
        const sent = structuredClone(draft);
        const reply = await post('/api/proxy/fallback', sent);
        confirmed = validFallback(reply.fallback) || sent;
        if (sameFallback(draft, sent)) draft = null;
      }
      draft = null;
      setSaving('已保存');
      showToast(pendingMessage, 'success');
    } catch (error) {
      draft = null;
      setSaving('');
      showToast(error.message || '处置顺序保存未完成', 'error', 5000);
    } finally {
      saving = false;
      renderChain();
    }
  }

  function renderLast(last) {
    const steps = Array.isArray(last?.steps) ? last.steps.filter(row => STEPS[row?.step]) : [];
    lastBox.hidden = !steps.length;
    if (!steps.length) return;
    lastTime.textContent = formatMoment(last.at);
    lastTime.dateTime = last.at || '';
    lastTime.title = parseDate(last.at)?.toLocaleString() || '';
    lastSteps.replaceChildren(...steps.map(row => {
      const result = RESULTS[row.result] ? row.result : 'skipped';
      const li = document.createElement('li');
      li.className = 'chain-last-step';
      li.dataset.result = result;
      li.innerHTML = `<span class="chain-result">${svg(RESULTS[result].path)}</span><span class="chain-last-name"></span><span class="sr-only"></span><span class="chain-last-detail"></span>`;
      li.querySelector('.chain-last-name').textContent = STEPS[row.step].title;
      li.querySelector('.sr-only').textContent = `，${RESULTS[result].text}`;
      const detail = li.querySelector('.chain-last-detail');
      detail.textContent = typeof row.detail === 'string' ? row.detail : '';
      detail.title = detail.textContent;
      return li;
    }));
  }

  // ── 对外 ─────────────────────────────────────────────────
  /** 用 /api/proxy 的状态刷新节点、处置顺序和上次处置。 */
  function render(status) {
    if (!status) return;
    guardActive = Boolean(status.enabled);
    autoRestore = Boolean(status.auto_restore);
    nodePaused = Boolean(status.node_switch_paused);
    const server = validFallback(status.fallback);
    if (server && !saving && !draft) confirmed = server;
    else if (server && !confirmed) confirmed = server;
    renderNode(status);
    renderChain();
    renderLast(status.last_fallback);
    renderSwitches();
  }

  /** 守护页可见时读取开关与订阅；force 用于打开页面或切换客户端后。 */
  function refreshLive({ force = false } = {}) {
    if (!visible() && !force) return Promise.resolve();
    return Promise.all([refreshSwitches(), refreshSubscription(force)]);
  }

  /** 切换客户端时清空上一个客户端的读数。 */
  function reset() {
    switchEpoch += 1;
    subEpoch += 1;
    switchPending = null;
    subPending = null;
    subFetchedAt = 0;
    switchData = null;
    renderSwitches();
    renderSubscription({ available: false, message: '正在读取…' });
    nodeCurrent.textContent = '正在读取…';
    nodeCurrent.dataset.state = 'empty';
  }

  renderChain();
  renderSwitches();
  return { render, refreshLive, reset };
}
