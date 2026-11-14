/**
 * NJUST WiFi 校园网认证 - 前端核心交互逻辑
 * 淡红淡蓝界面 · 原生 ES Module · 低资源消耗 · 隐藏页面停止轮询
 */

// ==========================================================================
// 状态管理
// ==========================================================================
import { initOnboarding } from './onboarding.js';
import { initUpdates } from './updates.js';
import { initAutoSave } from './autosave.js';
import { initWindow } from './window.js';
import { initProxy } from './proxy.js';
let autosave, proxyUI;

let sessionToken = null;
let pollTimer = null;
let isBusy = false;
let isServiceRunning = false;
let hasSavedCredentials = false;
let isRefreshing = false;
let configLoaded = false;
let configSaving = false;
let mutationPending = false;
let isRefreshingBalance = false;
let isBalanceLoading = false;
let lastProbeRender = null;
let lastLogRender = null;
let logClock = null;
let logsFetchedAt = 0;

// 备用网络列表有序数组缓存
let currentFallbackNetworks = [];

// 守护程序与日志状态缓存
let currentProbes = [];
let selectedProbeIds = new Set();
let candidateItems = [];
let selectedCandidatePaths = new Set();
let allLogEntries = [];
let currentLogFilter = '';
let currentLogDate = '';

// 通用确认弹窗回调
let pendingConfirmCallback = null;

// DOM 元素引用
const dom = {
  // 头部 & 主题
  versionText: document.getElementById('version-text'),
  connStatePill: document.getElementById('conn-state-pill'),
  btnThemeToggle: document.getElementById('btn-theme-toggle'),
  themeIconSvg: document.getElementById('theme-icon-svg'),
  themeIconText: document.getElementById('theme-icon-text'),
  toastBar: document.getElementById('toast-bar'),

  // 账户余额卡片
  balanceCard: document.getElementById('balance-card'),
  balanceVal: document.getElementById('balance-val'),
  balanceStatePill: document.getElementById('balance-state-pill'),
  balanceUpdatedText: document.getElementById('balance-updated-text'),
  btnRefreshBalance: document.getElementById('btn-refresh-balance'),

  // 核心状态卡片
  connectionCard: document.querySelector('.connection-card'),
  networkStatePill: document.getElementById('network-state-pill'),
  networkStateText: document.getElementById('network-state-text'),
  runningStatePill: document.getElementById('running-state-pill'),
  busyPill: document.getElementById('busy-pill'),
  stableInfoText: document.getElementById('stable-info-text'),
  statusMessage: document.getElementById('status-message'),
  btnToggleService: document.getElementById('btn-toggle-service'),
  btnCheckNow: document.getElementById('btn-check-now'),

  // 状态详情指标
  mNormalCount: document.getElementById('m-normal-count'),
  mStableSince: document.getElementById('m-stable-since'),
  mCheckedAt: document.getElementById('m-checked-at'),
  mNextCheckAt: document.getElementById('m-next-check-at'),
  btnExportDiag: document.getElementById('btn-export-diag'),

  // 分段选项卡
  tabBtns: document.querySelectorAll('.glass-segmented-tabs .seg-tab'),
  tabPanels: document.querySelectorAll('.tab-panel'),

  // 程序守护
  checkAllProbes: document.getElementById('check-all-probes'),
  probesCounter: document.getElementById('probes-counter'),
  btnOpenAddProbe: document.getElementById('btn-open-add-probe'),
  btnElevateProbes: document.getElementById('btn-elevate-probes'),
  btnRemoveProbes: document.getElementById('btn-remove-probes'),
  btnClearProbes: document.getElementById('btn-clear-probes'),
  probesList: document.getElementById('probes-list'),

  // 添加程序对话框
  modalAddProbe: document.getElementById('modal-add-probe'),
  btnCloseProbeModal: document.getElementById('btn-close-probe-modal'),
  btnBrowseFile: document.getElementById('btn-browse-file'),
  candidatesList: document.getElementById('candidates-list'),
  btnCancelAddProbe: document.getElementById('btn-cancel-add-probe'),
  btnConfirmAddProbe: document.getElementById('btn-confirm-add-probe'),

  // 参数设置
  formConfig: document.getElementById('form-config'),
  cfgSsid: document.getElementById('cfg-ssid'),
  cfgInterval: document.getElementById('cfg-interval'),
  // 备用网络列表元素
  fallbackCounter: document.getElementById('fallback-counter'),
  btnAddFallback: document.getElementById('btn-add-fallback'),
  fallbackNetworksList: document.getElementById('fallback-networks-list'),

  // 认证账号与密码
  savedAccountPill: document.getElementById('saved-account-pill'),
  cfgUsername: document.getElementById('cfg-username'),
  cfgPassword: document.getElementById('cfg-password'),
  btnToggleCampusPwd: document.getElementById('btn-toggle-campus-pwd'),
  cfgRemember: document.getElementById('cfg-remember'),
  cfgStartup: document.getElementById('cfg-startup'),
  cfgAutoMonitor: document.getElementById('cfg-auto-monitor'),

  // 高级设置项
  cfgDomain: document.getElementById('cfg-domain'),
  cfgTimeout: document.getElementById('cfg-timeout'),
  cfgChecks: document.getElementById('cfg-checks'),
  cfgShowPortal: document.getElementById('cfg-show-portal'),
  btnSaveConfig: document.getElementById('btn-save-config'),
  btnForgetCreds: document.getElementById('btn-forget-creds'),
  configFeedback: document.getElementById('config-feedback'),

  // 日志
  logSearchInput: document.getElementById('log-search-input'),
  logDateFilter: document.getElementById('log-date-filter'),
  btnClearSearch: document.getElementById('btn-clear-search'),
  btnOpenLogsDir: document.getElementById('btn-open-logs-dir'),
  btnClearLogs: document.getElementById('btn-clear-logs'),
  logsList: document.getElementById('logs-list'),

  // 确认对话框
  modalConfirm: document.getElementById('modal-confirm'),
  confirmDialogTitle: document.getElementById('confirm-dialog-title'),
  confirmDialogDesc: document.getElementById('confirm-dialog-desc'),
  btnConfirmCancel: document.getElementById('btn-confirm-cancel'),
  btnConfirmOk: document.getElementById('btn-confirm-ok')
};

// ==========================================================================
// 工具辅助函数
// ==========================================================================

/**
 * 格式化 ISO 时间为简洁本地时间
 */
function formatTime(isoString, includeDate = true) {
  if (!isoString) return '--';
  try {
    const d = new Date(isoString);
    if (Number.isNaN(d.getTime())) return String(isoString);

    const pad = (n) => String(n).padStart(2, '0');
    const hours = pad(d.getHours());
    const minutes = pad(d.getMinutes());
    const seconds = pad(d.getSeconds());

    if (!includeDate) {
      return `${hours}:${minutes}:${seconds}`;
    }

    const year = d.getFullYear();
    const month = pad(d.getMonth() + 1);
    const date = pad(d.getDate());
    return `${year}-${month}-${date} ${hours}:${minutes}:${seconds}`;
  } catch {
    return String(isoString);
  }
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (total < 60) return `${total}秒`;
  const days = Math.floor(total / 86400);
  const hours = Math.floor(total % 86400 / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  return [days ? `${days}天` : '', hours ? `${hours}时` : '', minutes ? `${minutes}分` : ''].filter(Boolean).join(' ');
}

let toastTimer = null;
/**
 * 浮动消息通知条
 */
function showToast(message, type = 'info', duration = 3000) {
  if (!dom.toastBar) return;
  clearTimeout(toastTimer);

  dom.toastBar.textContent = message;
  dom.toastBar.setAttribute('role', type === 'error' ? 'alert' : 'status');
  dom.toastBar.className = `toast-bar toast-${type}`;
  if (typeof dom.toastBar.showPopover === 'function') dom.toastBar.showPopover();

  toastTimer = setTimeout(() => {
    if (typeof dom.toastBar.hidePopover === 'function') dom.toastBar.hidePopover();
    dom.toastBar.className = 'toast-bar hidden';
    dom.toastBar.textContent = '';
  }, duration);
}

/**
 * 打开通用确认对话框
 */
function requestConfirm(title, desc, onConfirm) {
  pendingConfirmCallback = onConfirm;
  if (dom.confirmDialogTitle) dom.confirmDialogTitle.textContent = title;
  if (dom.confirmDialogDesc) dom.confirmDialogDesc.textContent = desc;

  if (dom.modalConfirm && typeof dom.modalConfirm.showModal === 'function') {
    dom.modalConfirm.showModal();
  } else {
    const ok = window.confirm(`${title}\n\n${desc}`);
    if (ok && typeof pendingConfirmCallback === 'function') {
      pendingConfirmCallback();
      pendingConfirmCallback = null;
    }
  }
}

function closeConfirmDialog() {
  if (dom.modalConfirm && typeof dom.modalConfirm.close === 'function') {
    dom.modalConfirm.close();
  }
  pendingConfirmCallback = null;
}

// ==========================================================================
// 密码显隐控制
// ==========================================================================

function createSvgIcon(kind, size = 15) {
  const svgNS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgNS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');

  const specs = {
    up: [['path', { d: 'M12 19V5' }], ['polyline', { points: '6 11 12 5 18 11' }]],
    down: [['path', { d: 'M12 5v14' }], ['polyline', { points: '6 13 12 19 18 13' }]],
    trash: [
      ['path', { d: 'M4 7h16' }],
      ['path', { d: 'M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7' }],
      ['path', { d: 'M7 7l1 12a2 2 0 0 0 2 1.8h4a2 2 0 0 0 2-1.8l1-12' }]
    ],
    app: [
      ['rect', { x: '4', y: '4', width: '16', height: '16', rx: '4' }],
      ['path', { d: 'M9 12h6M12 9v6' }]
    ],
    shield: [
      ['path', { d: 'M12 3 4.5 6.2V12c0 4.6 3.2 8.2 7.5 9.5 4.3-1.3 7.5-4.9 7.5-9.5V6.2z' }],
      ['path', { d: 'm9 12.2 2 2.2 4-4.4' }]
    ],
    log: [
      ['rect', { x: '5', y: '3.5', width: '14', height: '17', rx: '3' }],
      ['path', { d: 'M9 8.5h6M9 12h6M9 15.5h4' }]
    ],
    moon: [['path', { d: 'M20.5 14.5A8.5 8.5 0 1 1 9.5 3.5a7 7 0 0 0 11 11z' }]],
    sun: [
      ['circle', { cx: '12', cy: '12', r: '4' }],
      ['path', { d: 'M12 2.5v2M12 19.5v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2.5 12h2M19.5 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4' }]
    ]
  };

  for (const [tag, attrs] of specs[kind] || []) {
    const node = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      node.setAttribute(k, v);
    }
    svg.appendChild(node);
  }
  return svg;
}

function createEmptyStateItem(text, iconKind) {
  const li = document.createElement('li');
  li.className = 'item-empty';
  if (iconKind) {
    const badge = document.createElement('span');
    badge.className = 'empty-badge-icon';
    badge.setAttribute('aria-hidden', 'true');
    badge.appendChild(createSvgIcon(iconKind, 20));
    li.appendChild(badge);
  }
  const label = document.createElement('span');
  label.textContent = text;
  li.appendChild(label);
  return li;
}

function updatePasswordEyeIcon(buttonEl, isVisible) {
  while (buttonEl.firstChild) {
    buttonEl.removeChild(buttonEl.firstChild);
  }

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('aria-hidden', 'true');

  if (isVisible) {
    // 闭眼/斜杠图标
    const p1 = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    p1.setAttribute('d', 'M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24');
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', '1');
    line.setAttribute('y1', '1');
    line.setAttribute('x2', '23');
    line.setAttribute('y2', '23');
    svg.appendChild(p1);
    svg.appendChild(line);
  } else {
    // 开眼图标
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z');
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', '12');
    circle.setAttribute('cy', '12');
    circle.setAttribute('r', '3');
    svg.appendChild(path);
    svg.appendChild(circle);
  }

  buttonEl.appendChild(svg);
}

function togglePasswordInput(inputEl, buttonEl) {
  if (!inputEl || !buttonEl) return;
  const isCurrentlyPassword = inputEl.type === 'password';
  inputEl.type = isCurrentlyPassword ? 'text' : 'password';
  const label = buttonEl.dataset.passwordLabel || '密码';
  buttonEl.setAttribute('aria-label', (isCurrentlyPassword ? '隐藏' : '显示') + label);
  buttonEl.setAttribute('aria-pressed', String(isCurrentlyPassword));
  buttonEl.title = isCurrentlyPassword ? '隐藏密码' : '显示密码';
  updatePasswordEyeIcon(buttonEl, isCurrentlyPassword);
}

// ==========================================================================
// API 网络通信层
// ==========================================================================

/**
 * 获取或更新会话 Token
 */
async function fetchSessionToken() {
  try {
    const res = await fetch('/api/session', {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
      cache: 'no-store'
    });
    if (!res.ok) throw new Error(`获取会话失败 (${res.status})`);
    const data = await res.json();
    if (data && data.token) {
      sessionToken = data.token;
      return sessionToken;
    }
    throw new Error('未获取到会话凭证');
  } catch (err) {
    console.error('Session 认证异常:', err);
    throw err;
  }
}

/**
 * 底层同源 API 请求封装
 */
async function sendApiRequest(endpoint, options = {}, isRetry = false) {
  if (!sessionToken) {
    await fetchSessionToken();
  }

  const reqOptions = { ...options };
  reqOptions.cache = 'no-store';
  reqOptions.headers = {
    'Accept': 'application/json',
    ...(reqOptions.headers || {})
  };

  if (sessionToken) {
    reqOptions.headers['X-NJUST-Token'] = sessionToken;
  }

  let res;
  try {
    res = await fetch(endpoint, reqOptions);
  } catch (netErr) {
    if (dom.connStatePill) {
      dom.connStatePill.textContent = '离线 / 未连接';
      dom.connStatePill.className = 'glass-pill pill-busy';
    }
    throw new Error('网络请求异常，请确认服务运行中');
  }

  if (dom.connStatePill) {
    dom.connStatePill.textContent = '服务就绪';
    dom.connStatePill.className = 'glass-pill pill-soft';
  }

  // 401/403 重新拉取 token 并重试一次
  if ((res.status === 401 || res.status === 403) && !isRetry) {
    sessionToken = null;
    await fetchSessionToken();
    return apiRequest(endpoint, options, true);
  }

  if (!res.ok) {
    let errMsg = `请求失败 (${res.status})`;
    try {
      const errJson = await res.json();
      if (errJson && errJson.error) errMsg = errJson.error;
    } catch {
      // ignore
    }
    throw new Error(errMsg);
  }

  return res.json();
}

/**
 * 包装请求排队与状态互斥
 */
async function apiRequest(endpoint, options = {}, isRetry = false) {
  const mutation = options.method === 'POST' && !isRetry;
  if (mutation && mutationPending) throw new Error('请等待当前操作完成');
  if (mutation) { mutationPending = true; syncControls(); }
  try {
    return await sendApiRequest(endpoint, options, isRetry);
  } finally {
    if (mutation) { mutationPending = false; syncControls(); }
  }
}

function syncControls() {
  const pending = mutationPending || configSaving || !configLoaded;
  if (dom.btnToggleService) dom.btnToggleService.disabled = pending;
  if (dom.btnCheckNow) dom.btnCheckNow.disabled = pending || isBusy;
  if (dom.btnSaveConfig) dom.btnSaveConfig.disabled = pending || isBusy;
  if (dom.btnExportDiag) dom.btnExportDiag.disabled = pending;
  if (dom.btnForgetCreds) {
    dom.btnForgetCreds.disabled = pending || isBusy || isServiceRunning || !hasSavedCredentials;
  }
  if (dom.btnBrowseFile) dom.btnBrowseFile.disabled = mutationPending;
  if (dom.btnConfirmAddProbe) {
    dom.btnConfirmAddProbe.disabled = mutationPending || selectedCandidatePaths.size === 0;
  }
  if (dom.btnRefreshBalance) {
    dom.btnRefreshBalance.disabled = isRefreshingBalance || isBalanceLoading || mutationPending;
  }
  if (dom.btnAddFallback) {
    dom.btnAddFallback.disabled = pending || currentFallbackNetworks.length >= 8;
  }

  dom.fallbackNetworksList.querySelectorAll('.fallback-network-row').forEach((row, index, rows) => {
    const buttons = row.querySelectorAll('.btn-mini-action');
    buttons.forEach(button => { button.disabled = pending; });
    if (!pending) { buttons[0].disabled = index === 0; buttons[1].disabled = index === rows.length - 1; }
  });
  updateProbesToolbar();
}

// ==========================================================================
// 余额查询与呈现 (Balance Feature)
// ==========================================================================

async function loadBalance() {
  try {
    const data = await apiRequest('/api/balance');
    renderBalance(data);
  } catch (err) {
    console.warn('获取余额失败:', err.message);
  }
}

function renderBalance(data) {
  if (!data) return;
  isBalanceLoading = data.state === 'loading';
  syncControls();

  // 余额数值：null 显示 "—"，不可当作 0
  if (dom.balanceVal) {
    if (data.balance === null || data.balance === undefined) {
      dom.balanceVal.textContent = '—';
    } else if (typeof data.balance === 'number' && Number.isFinite(data.balance)) {
      dom.balanceVal.textContent = `¥ ${data.balance.toFixed(2)}`;
    } else {
      dom.balanceVal.textContent = '—';
    }
  }

  // 状态状态药丸徽章
  if (dom.balanceStatePill) {
    if (data.state === 'loading') {
      dom.balanceStatePill.textContent = '查询中';
      dom.balanceStatePill.className = 'glass-status-pill state-checking';
    } else if (data.state === 'ready') {
      dom.balanceStatePill.textContent = '就绪';
      dom.balanceStatePill.className = 'glass-status-pill state-online';
    } else {
      dom.balanceStatePill.textContent = data.updated_at ? '待更新' : '未获取';
      dom.balanceStatePill.className = 'glass-status-pill state-idle';
    }
  }

  // 更新时间与错误短提示
  if (dom.balanceUpdatedText) {
    if (data.state === 'unavailable' && data.message) {
      dom.balanceUpdatedText.textContent = data.message;
      dom.balanceUpdatedText.title = data.message;
    } else if (data.updated_at) {
      dom.balanceUpdatedText.textContent = `更新于 ${formatTime(data.updated_at, false)}`;
      dom.balanceUpdatedText.title = formatTime(data.updated_at, true);
    } else {
      dom.balanceUpdatedText.textContent = '未更新';
      dom.balanceUpdatedText.title = '';
    }
  }
}

async function handleRefreshBalance() {
  if (isRefreshingBalance || isBalanceLoading || mutationPending) return;
  try {
    isRefreshingBalance = true;
    syncControls();
    showToast('正在请求刷新余额...', 'info');

    if (dom.balanceStatePill) {
      dom.balanceStatePill.textContent = '查询中';
      dom.balanceStatePill.className = 'glass-status-pill state-checking';
    }

    await apiRequest('/api/balance/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });

    await loadBalance();
    showToast('已请求刷新', 'info');
  } catch (err) {
    showToast(`刷新余额失败: ${err.message}`, 'error');
  } finally {
    isRefreshingBalance = false;
    syncControls();
  }
}

// ==========================================================================
// 状态与指标更新
// ==========================================================================

async function loadStatus() {
  try {
    const data = await apiRequest('/api/status');
    renderStatus(data);
  } catch (err) {
    if (dom.statusMessage) dom.statusMessage.textContent = `状态获取异常: ${err.message}`;
  }
}

function renderStatus(status) {
  if (!status) return;

  isBusy = Boolean(status.busy);
  isServiceRunning = Boolean(status.running);

  if (status.version && dom.versionText) {
    dom.versionText.textContent = `v${status.version}`;
  }

  if (dom.busyPill) {
    if (isBusy) dom.busyPill.classList.remove('hidden');
    else dom.busyPill.classList.add('hidden');
  }

  if (dom.runningStatePill) {
    if (status.running) {
      dom.runningStatePill.textContent = '运行中';
      dom.runningStatePill.className = 'glass-pill pill-soft';
    } else {
      dom.runningStatePill.textContent = '已暂停';
      dom.runningStatePill.className = 'glass-pill pill-subtle';
    }
  }

  if (dom.btnToggleService) {
    const labelSpan = dom.btnToggleService.querySelector('.btn-label') || dom.btnToggleService;
    if (status.running || isBusy) {
      labelSpan.textContent = status.running ? '暂停监控' : '取消检测';
    } else {
      labelSpan.textContent = '开始监控';
    }
  }

  const stateMap = {
    online: { text: '在线正常', pillClass: 'glass-status-pill state-online' },
    checking: { text: '正在检测', pillClass: 'glass-status-pill state-checking' },
    recovering: { text: '自愈重连', pillClass: 'glass-status-pill state-recovering' },
    error: { text: '认证异常', pillClass: 'glass-status-pill state-error' },
    idle: { text: '待机静默', pillClass: 'glass-status-pill state-idle' }
  };

  const currentSt = stateMap[status.state] || {
    text: status.state || '未知',
    pillClass: 'glass-status-pill state-idle'
  };

  if (dom.networkStatePill) dom.networkStatePill.className = currentSt.pillClass;
  if (dom.networkStateText) dom.networkStateText.textContent = currentSt.text;
  if (dom.connectionCard) dom.connectionCard.dataset.state = status.state || 'idle';
  const pathLabel = document.getElementById('summary-path');
  if (pathLabel) pathLabel.textContent = ({normal: '常规直连', 'wifi-direct': 'WiFi 直连', unavailable: '待重试'})[status.connection_path] || '待检测';

  let stableDesc = '';
  if (status.unchanged_since) {
    stableDesc = `稳定自 ${formatTime(status.unchanged_since, false)}`;
  }
  if (status.normal_count) {
    stableDesc += (stableDesc ? ' · ' : '') + `连续正常 ${status.normal_count} 次`;
  }
  if (dom.stableInfoText) {
    dom.stableInfoText.textContent = stableDesc || '网络待命';
  }

  if (dom.statusMessage) {
    dom.statusMessage.textContent = status.message || '系统运行正常';
  }

  if (dom.mNormalCount) dom.mNormalCount.textContent = `${status.normal_count ?? 0} 次`;
  if (dom.mStableSince) dom.mStableSince.textContent = status.unchanged_since ? formatTime(status.unchanged_since) : '--';
  if (dom.mCheckedAt) dom.mCheckedAt.textContent = formatTime(status.checked_at);
  if (dom.mNextCheckAt) dom.mNextCheckAt.textContent = formatTime(status.next_check_at);
  syncControls();
}

// ==========================================================================
// 备用 WiFi 有序列表管理
// ==========================================================================

/**
 * 将当前 DOM 上的备用网络输入同步回内存数组
 */
function syncFallbackNetworksFromDOM() {
  if (!dom.fallbackNetworksList) return;
  const rows = dom.fallbackNetworksList.querySelectorAll('.fallback-network-row');
  const synced = [];

  rows.forEach((row, i) => {
    const ssidInput = row.querySelector('.fallback-ssid-input');
    const pwdInput = row.querySelector('.fallback-pwd-input');
    const profilePassword = currentFallbackNetworks[i]?.profilePassword || '';
    synced.push({
      ssid: ssidInput ? ssidInput.value : (currentFallbackNetworks[i]?.ssid || ''),
      password: pwdInput ? (profilePassword && pwdInput.value === profilePassword ? '' : pwdInput.value) : (currentFallbackNetworks[i]?.password || ''),
      profilePassword
    });
  });

  if (synced.length > 0 || currentFallbackNetworks.length === 0) {
    currentFallbackNetworks = synced;
  }
}

/**
 * 渲染备用网络列表
 */
function renderFallbackNetworks() {
  if (!dom.fallbackNetworksList) return;

  while (dom.fallbackNetworksList.firstChild) {
    dom.fallbackNetworksList.removeChild(dom.fallbackNetworksList.firstChild);
  }

  const total = currentFallbackNetworks.length;
  if (dom.fallbackCounter) {
    dom.fallbackCounter.textContent = `${total} / 8`;
  }

  if (total === 0) {
    const emptyLi = document.createElement('li');
    emptyLi.className = 'item-empty';
    emptyLi.textContent = '暂无备用 WiFi';
    dom.fallbackNetworksList.appendChild(emptyLi);
  } else {
    currentFallbackNetworks.forEach((item, index) => {
      const li = document.createElement('li');
      li.className = 'fallback-network-row';

      // 顶部行：优先级标记与操作按钮
      const headerRow = document.createElement('div');
      headerRow.className = 'fallback-row-header';

      const priorityBadge = document.createElement('span');
      priorityBadge.className = 'fallback-priority-badge';
      priorityBadge.textContent = `#${index + 1}`;
      priorityBadge.title = `优先级 #${index + 1}`;
      headerRow.appendChild(priorityBadge);

      const actionsGroup = document.createElement('div');
      actionsGroup.className = 'fallback-actions-group';

      // 上移按钮 (第一项禁用)
      const btnUp = document.createElement('button');
      btnUp.type = 'button';
      btnUp.className = 'btn-mini-action';
      btnUp.title = '上移';
      btnUp.setAttribute('aria-label', `上移第 ${index + 1} 个备用网络`);
      btnUp.appendChild(createSvgIcon('up', 14));
      btnUp.disabled = index === 0 || mutationPending;
      btnUp.addEventListener('click', () => moveFallbackNetwork(index, -1));
      actionsGroup.appendChild(btnUp);

      // 下移按钮 (最后一项禁用)
      const btnDown = document.createElement('button');
      btnDown.type = 'button';
      btnDown.className = 'btn-mini-action';
      btnDown.title = '下移';
      btnDown.setAttribute('aria-label', `下移第 ${index + 1} 个备用网络`);
      btnDown.appendChild(createSvgIcon('down', 14));
      btnDown.disabled = index === total - 1 || mutationPending;
      btnDown.addEventListener('click', () => moveFallbackNetwork(index, 1));
      actionsGroup.appendChild(btnDown);

      // 删除按钮
      const btnDel = document.createElement('button');
      btnDel.type = 'button';
      btnDel.className = 'btn-mini-action btn-del-text';
      btnDel.title = '删除';
      btnDel.setAttribute('aria-label', `删除第 ${index + 1} 个备用网络`);
      btnDel.appendChild(createSvgIcon('trash', 14));
      btnDel.disabled = mutationPending;
      btnDel.addEventListener('click', () => deleteFallbackNetwork(index));
      actionsGroup.appendChild(btnDel);

      headerRow.appendChild(actionsGroup);
      li.appendChild(headerRow);

      // 输入框网格：SSID 与 密码
      const fieldsGrid = document.createElement('div');
      fieldsGrid.className = 'fallback-fields-grid';

      // SSID 槽
      const ssidField = document.createElement('div');
      ssidField.className = 'fallback-field';

      const ssidLabel = document.createElement('label');
      ssidLabel.className = 'slot-label';
      ssidLabel.textContent = '网络名称 (SSID)';
      ssidField.appendChild(ssidLabel);

      const ssidInput = document.createElement('input');
      ssidInput.type = 'text';
      ssidInput.id = `fallback-ssid-${index}`;
      ssidLabel.htmlFor = ssidInput.id;
      ssidInput.className = 'glass-input fallback-ssid-input';
      ssidInput.maxLength = 32;
      ssidInput.placeholder = '备用网络 SSID';
      ssidInput.value = item.ssid || '';
      ssidInput.setAttribute('aria-label', `第 ${index + 1} 个备用网络的 SSID`);
      ssidInput.autocomplete = 'off';
      ssidInput.addEventListener('input', () => {
        if (item.profilePassword && item.ssid !== ssidInput.value) {
          item.profilePassword = '';
          pwdInput.value = '';
        }
        item.ssid = ssidInput.value;
      });
      ssidField.appendChild(ssidInput);
      fieldsGrid.appendChild(ssidField);

      // 密码槽带显隐按钮
      const pwdField = document.createElement('div');
      pwdField.className = 'fallback-field';

      const pwdLabel = document.createElement('label');
      pwdLabel.className = 'slot-label';
      pwdLabel.textContent = '网络密码';
      pwdField.appendChild(pwdLabel);

      const pwdWrap = document.createElement('div');
      pwdWrap.className = 'password-input-group';

      const pwdInput = document.createElement('input');
      pwdInput.type = 'text';
      pwdInput.id = `fallback-password-${index}`;
      pwdLabel.htmlFor = pwdInput.id;
      pwdInput.className = 'glass-input input-with-toggle fallback-pwd-input';
      pwdInput.placeholder = '留空复用本机网络配置';
      pwdInput.value = item.password || item.profilePassword || '';
      if (item.profilePassword) pwdInput.title = 'Windows 已保存的网络密码';
      pwdInput.setAttribute('aria-label', `第 ${index + 1} 个备用网络的密码`);
      pwdInput.autocomplete = 'new-password';
      pwdInput.addEventListener('input', () => {
        item.profilePassword = '';
        pwdInput.removeAttribute('title');
        item.password = pwdInput.value;
      });
      pwdWrap.appendChild(pwdInput);

      const toggleBtn = document.createElement('button');
      toggleBtn.type = 'button';
      toggleBtn.className = 'btn-toggle-eye';
      toggleBtn.setAttribute('aria-label', `隐藏第 ${index + 1} 个备用网络密码`);
      toggleBtn.setAttribute('aria-pressed', 'true');
      toggleBtn.dataset.passwordLabel = `第 ${index + 1} 个备用网络密码`;
      toggleBtn.title = '显示/隐藏密码';
      updatePasswordEyeIcon(toggleBtn, true);
      toggleBtn.addEventListener('click', () => {
        togglePasswordInput(pwdInput, toggleBtn);
      });
      pwdWrap.appendChild(toggleBtn);

      pwdField.appendChild(pwdWrap);
      fieldsGrid.appendChild(pwdField);

      li.appendChild(fieldsGrid);
      dom.fallbackNetworksList.appendChild(li);
    });
  }

  if (dom.btnAddFallback) {
    dom.btnAddFallback.disabled = total >= 8 || mutationPending;
  }
}

function addFallbackNetwork() {
  if (configSaving || mutationPending) return;
  syncFallbackNetworksFromDOM();
  if (currentFallbackNetworks.length >= 8) {
    showToast('备用网络最多设置 8 个', 'error');
    return;
  }

  currentFallbackNetworks.push({ ssid: '', password: '' });
  renderFallbackNetworks();

  // 聚焦新增条目的 SSID 输入框
  const rows = dom.fallbackNetworksList.querySelectorAll('.fallback-network-row');
  if (rows.length > 0) {
    const lastRowInput = rows[rows.length - 1].querySelector('.fallback-ssid-input');
    if (lastRowInput) lastRowInput.focus();
  }
}

function deleteFallbackNetwork(index) {
  if (configSaving || mutationPending) return;
  syncFallbackNetworksFromDOM();
  if (index >= 0 && index < currentFallbackNetworks.length) {
    currentFallbackNetworks.splice(index, 1);
    renderFallbackNetworks();
    autosave?.queueFallbacks();
  }
}

function moveFallbackNetwork(index, direction) {
  if (configSaving || mutationPending) return;
  syncFallbackNetworksFromDOM();
  const targetIndex = index + direction;
  if (targetIndex < 0 || targetIndex >= currentFallbackNetworks.length) return;

  const temp = currentFallbackNetworks[index];
  currentFallbackNetworks[index] = currentFallbackNetworks[targetIndex];
  currentFallbackNetworks[targetIndex] = temp;
  renderFallbackNetworks();
  autosave?.queueFallbacks();

  // 聚焦移动后所在位置的输入框
  const rows = dom.fallbackNetworksList.querySelectorAll('.fallback-network-row');
  if (rows[targetIndex]) {
    const input = rows[targetIndex].querySelector('.fallback-ssid-input');
    if (input) input.focus();
  }
}

// ==========================================================================
// 守护程序管理 (Probes)
// ==========================================================================

async function loadProbes() {
  try {
    const data = await apiRequest('/api/probes');
    currentProbes = (data && Array.isArray(data.probes)) ? data.probes : [];

    const validIds = new Set(currentProbes.map(p => p.id));
    for (const id of selectedProbeIds) {
      if (!validIds.has(id)) {
        selectedProbeIds.delete(id);
      }
    }

    renderProbesList();
  } catch (err) {
    console.warn('获取守护程序失败:', err.message);
  }
}

function renderProbesList() {
  if (!dom.probesList) return;
  const summary = document.getElementById('summary-probes');
  if (summary) summary.textContent = `${currentProbes.length} 个已添加`;
  const signature = JSON.stringify([currentProbes, [...selectedProbeIds]]);
  if (signature === lastProbeRender) { updateProbesToolbar(); return; }
  lastProbeRender = signature;

  while (dom.probesList.firstChild) {
    dom.probesList.removeChild(dom.probesList.firstChild);
  }

  if (currentProbes.length === 0) {
    dom.probesList.appendChild(createEmptyStateItem('暂无守护程序', 'shield'));
  } else {
    for (const probe of currentProbes) {
      const li = document.createElement('li');
      li.className = 'probe-row';
      li.title = probe.path || '';

      const left = document.createElement('label');
      left.className = 'probe-left';

      const chk = document.createElement('input');
      chk.type = 'checkbox';
      chk.className = 'glass-checkbox';
      chk.checked = selectedProbeIds.has(probe.id);
      chk.setAttribute('aria-label', probe.name || '守护程序');
      chk.addEventListener('change', () => {
        if (chk.checked) {
          selectedProbeIds.add(probe.id);
        } else {
          selectedProbeIds.delete(probe.id);
        }
        updateProbesToolbar();
      });
      left.appendChild(chk);

      const appBadge = document.createElement('span');
      appBadge.className = 'probe-app-badge';
      appBadge.setAttribute('aria-hidden', 'true');
      appBadge.appendChild(createSvgIcon('app', 15));
      left.appendChild(appBadge);

      const textCol = document.createElement('div');
      textCol.className = 'probe-text-col';

      const nameBox = document.createElement('div');
      nameBox.className = 'probe-name-box';

      const nameSpan = document.createElement('span');
      nameSpan.className = 'probe-name';
      nameSpan.textContent = probe.name || '未命名应用';
      nameBox.appendChild(nameSpan);

      if (probe.elevated) {
        const adminBadge = document.createElement('span');
        adminBadge.className = 'probe-admin-pill';
        adminBadge.textContent = '管理员';
        nameBox.appendChild(adminBadge);
      }

      textCol.appendChild(nameBox);

      if (probe.path) {
        const pathSpan = document.createElement('span');
        pathSpan.className = 'probe-path';
        pathSpan.textContent = probe.path;
        textCol.appendChild(pathSpan);
      }

      left.appendChild(textCol);
      li.appendChild(left);

      const route = document.createElement('select');
      route.className = 'glass-input probe-route'; route.setAttribute('aria-label', `${probe.name} 的网络方式`);
      for (const [value, text] of [['follow','跟随系统'],['direct','直连'],['proxy','代理 / TUN']]) {
        const option = new Option(text, value); route.add(option);
      }
      route.value = probe.routing || 'follow';
      route.addEventListener('change', async () => {
        route.disabled = true;
        try { await apiRequest('/api/proxy/policy', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:probe.id, mode:route.value})}); showToast('网络方式已设置', 'success'); }
        catch(error) { route.value = probe.routing || 'follow'; showToast(error.message, 'error'); }
        finally { route.disabled = false; await loadProbes(); proxyUI?.refresh(); }
      });
      li.appendChild(route);
      const stateSpan = document.createElement('span');
      const stateMap = {
        running: { text: '运行中', cls: 'state-running' },
        started: { text: '已启动', cls: 'state-started' },
        error: { text: '异常', cls: 'state-error' },
        unknown: { text: '未知', cls: 'state-unknown' }
      };
      const st = stateMap[probe.state] || { text: probe.state || '未知', cls: 'state-unknown' };
      stateSpan.className = `probe-state-pill ${st.cls}`;
      stateSpan.textContent = st.text;
      li.appendChild(stateSpan);

      dom.probesList.appendChild(li);
    }
  }

  updateProbesToolbar();
}

function updateProbesToolbar() {
  const total = currentProbes.length;
  const selected = selectedProbeIds.size;

  if (dom.probesCounter) {
    dom.probesCounter.textContent = `${selected}/${total} 项`;
  }

  if (dom.checkAllProbes) {
    dom.checkAllProbes.checked = total > 0 && selected === total;
    dom.checkAllProbes.indeterminate = selected > 0 && selected < total;
  }

  if (dom.btnRemoveProbes) {
    dom.btnRemoveProbes.disabled = selected === 0 || isBusy || mutationPending;
  }
  if (dom.btnElevateProbes) {
    dom.btnElevateProbes.disabled = selected === 0 || isBusy || mutationPending;
  }
  if (dom.btnClearProbes) {
    dom.btnClearProbes.disabled = total === 0 || isBusy || mutationPending;
  }
  if (dom.btnOpenAddProbe) {
    dom.btnOpenAddProbe.disabled = isBusy || mutationPending;
  }
}

// 守护程序操作行为
async function openAddProbeModal() {
  if (isBusy) return;
  selectedCandidatePaths.clear();
  candidateItems = [];
  renderCandidatesList();

  if (dom.modalAddProbe && typeof dom.modalAddProbe.showModal === 'function') {
    dom.modalAddProbe.showModal();
  }

  try {
    const data = await apiRequest('/api/probes/candidates');
    candidateItems = (data && Array.isArray(data.candidates)) ? data.candidates : [];
    renderCandidatesList();
  } catch (err) {
    showToast(`获取候选程序失败: ${err.message}`, 'error');
  }
}

function closeAddProbeModal() {
  if (dom.modalAddProbe && typeof dom.modalAddProbe.close === 'function') {
    dom.modalAddProbe.close();
  }
}

async function handleBrowseFile() {
  try {
    showToast('已唤起系统文件选择窗口...', 'info');
    const data = await apiRequest('/api/probes/browse', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });

    const newCandidates = (data && Array.isArray(data.candidates)) ? data.candidates : [];
    if (newCandidates.length > 0) {
      for (const c of newCandidates) {
        if (!candidateItems.some(item => item.path === c.path)) {
          candidateItems.unshift(c);
        }
        selectedCandidatePaths.add(c.path);
      }
      renderCandidatesList();
      showToast(`已选择 ${newCandidates.length} 个文件`, 'success');
    }
  } catch (err) {
    showToast(`浏览文件失败: ${err.message}`, 'error');
  }
}

function renderCandidatesList() {
  if (!dom.candidatesList) return;
  syncControls();

  while (dom.candidatesList.firstChild) {
    dom.candidatesList.removeChild(dom.candidatesList.firstChild);
  }

  if (candidateItems.length === 0) {
    const li = document.createElement('li');
    li.className = 'item-empty';
    li.textContent = '暂无候选程序';
    dom.candidatesList.appendChild(li);
    return;
  }

  for (const c of candidateItems) {
    const li = document.createElement('li');
    li.className = 'candidate-item';

    const label = document.createElement('label');
    label.className = 'candidate-label';

    const chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.className = 'glass-checkbox';
    chk.checked = selectedCandidatePaths.has(c.path);
    chk.setAttribute('aria-label', c.name || '候选程序');
    chk.addEventListener('change', () => {
      if (chk.checked) selectedCandidatePaths.add(c.path);
      else selectedCandidatePaths.delete(c.path);
      syncControls();
    });
    label.appendChild(chk);

    const info = document.createElement('div');
    info.className = 'candidate-info';

    const name = document.createElement('span');
    name.className = 'candidate-name';
    name.textContent = c.name || '应用';
    info.appendChild(name);

    const path = document.createElement('span');
    path.className = 'candidate-path';
    path.textContent = c.path || '';
    info.appendChild(path);

    label.appendChild(info);
    li.appendChild(label);
    dom.candidatesList.appendChild(li);
  }
}

async function handleConfirmAddProbe() {
  const paths = Array.from(selectedCandidatePaths);
  if (paths.length === 0) {
    showToast('请至少勾选一个候选程序', 'error');
    return;
  }

  try {
    dom.btnConfirmAddProbe.disabled = true;
    await apiRequest('/api/probes/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths })
    });
    showToast('已添加守护程序', 'success');
    closeAddProbeModal();
    await loadProbes();
  } catch (err) {
    showToast(`添加失败: ${err.message}`, 'error');
  } finally {
    dom.btnConfirmAddProbe.disabled = false;
  }
}

function handleRemoveSelectedProbes() {
  if (selectedProbeIds.size === 0 || isBusy) return;
  const count = selectedProbeIds.size;
  requestConfirm(
    '确认移除守护程序',
    `确定要移除选中的 ${count} 个守护程序吗？`,
    async () => {
      try {
        await apiRequest('/api/probes/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ids: Array.from(selectedProbeIds) })
        });
        selectedProbeIds.clear();
        showToast('已移除选定程序', 'success');
        await loadProbes();
      } catch (err) {
        showToast(`移除失败: ${err.message}`, 'error');
      }
    }
  );
}

function handleClearAllProbes() {
  if (currentProbes.length === 0 || isBusy) return;
  requestConfirm(
    '清空全部守护程序',
    '确定要清空全部守护程序吗？已守护的应用将被全部解绑。',
    async () => {
      try {
        await apiRequest('/api/probes/clear', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        selectedProbeIds.clear();
        showToast('守护列表已清空', 'success');
        await loadProbes();
      } catch (err) {
        showToast(`清空失败: ${err.message}`, 'error');
      }
    }
  );
}

async function handleElevateSelectedProbes() {
  if (selectedProbeIds.size === 0 || isBusy) return;
  try {
    dom.btnElevateProbes.disabled = true;
    await apiRequest('/api/probes/elevate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: Array.from(selectedProbeIds) })
    });
    showToast('已请求以管理员权限启动程序', 'success');
    await loadProbes();
  } catch (err) {
    showToast(`提权启动失败: ${err.message}`, 'error');
  } finally {
    dom.btnElevateProbes.disabled = false;
  }
}

// ==========================================================================
// 凭据读取与配置管理 (Credentials & Config)
// ==========================================================================

/**
 * 本地会话保护接口获取已存凭据并在前端真实回填
 */
async function loadCredentials() {
  try {
    const data = await apiRequest('/api/credentials');
    if (data) {
      if (dom.cfgUsername && data.username !== undefined) {
        dom.cfgUsername.value = data.username || '';
      }
      if (dom.cfgPassword && data.password !== undefined) {
        dom.cfgPassword.value = data.password || '';
      }
    }
  } catch (err) {
    console.warn('读取已存凭据失败:', err.message);
  }
}

async function loadConfig() {
  try {
    const data = await apiRequest('/api/config');
    if (!data) return;

    if (dom.cfgSsid) dom.cfgSsid.value = data.ssid ?? '';
    const campusSummary = document.getElementById('summary-campus');
    if (campusSummary) campusSummary.textContent = data.ssid || '未设置';
    const fallbackSummary = document.getElementById('summary-fallbacks');
    if (fallbackSummary) fallbackSummary.textContent = `${data.fallback_networks?.length || 0} 个已配置`;
    if (dom.cfgInterval) dom.cfgInterval.value = data.interval_minutes ?? 10;
    if (dom.cfgRemember) dom.cfgRemember.checked = Boolean(data.remember_credentials);
    if (dom.cfgStartup) dom.cfgStartup.checked = Boolean(data.startup_enabled);
    if (dom.cfgAutoMonitor) dom.cfgAutoMonitor.checked = Boolean(data.auto_start_monitor);

    // 认证域、HTTP超时、探测地址、认证页显示
    if (dom.cfgDomain) dom.cfgDomain.value = data.domain || 'default';
    if (dom.cfgTimeout) dom.cfgTimeout.value = data.connectivity_timeout_ms ?? 5000;
    if (dom.cfgChecks) dom.cfgChecks.value = data.connectivity_checks ?? '';
    if (dom.cfgShowPortal) dom.cfgShowPortal.checked = Boolean(data.show_portal);
    document.getElementById('cfg-close-behavior').value = data.close_behavior || 'ask';

    // 备用网络有序数组回显
    currentFallbackNetworks = Array.isArray(data.fallback_networks)
      ? data.fallback_networks.map(item => ({
          ssid: item.ssid || '',
          password: item.password || '',
          profilePassword: item.profile_password || ''
        }))
      : [];
    renderFallbackNetworks();

    hasSavedCredentials = Boolean(data.has_credentials);
    const masked = data.username_masked || '';

    if (dom.savedAccountPill) {
      if (hasSavedCredentials) {
        dom.savedAccountPill.textContent = `已存账号: ${masked || '已配置'}`;
        dom.savedAccountPill.className = 'glass-pill pill-soft';
      } else {
        dom.savedAccountPill.textContent = '未设置账号';
        dom.savedAccountPill.className = 'glass-pill pill-subtle';
      }
    }

    if (dom.btnForgetCreds) {
      dom.btnForgetCreds.disabled = !hasSavedCredentials || isServiceRunning || isBusy;
    }
    configLoaded = true;
    syncControls();
  } catch (err) {
    console.error('加载设置失败:', err);
  }
}

// ==========================================================================
// 日志系统 (Logs)
// ==========================================================================

async function loadLogs() {
  try {
    const data = await apiRequest('/api/logs');
    allLogEntries = (data && Array.isArray(data.entries)) ? data.entries : [];
    logsFetchedAt = performance.now();
    renderLogsList();
  } catch (err) {
    console.warn('获取日志失败:', err.message);
  }
}

function renderLogsList() {
  if (!dom.logsList) return;
  const signature = JSON.stringify([allLogEntries.map(entry => entry.active ? {...entry, duration_seconds: 0} : entry), currentLogFilter, currentLogDate]);
  if (signature === lastLogRender) {
    updateLogDurations();
    syncLogClock();
    return;
  }
  lastLogRender = signature;
  syncLogClock();

  while (dom.logsList.firstChild) {
    dom.logsList.removeChild(dom.logsList.firstChild);
  }

  const query = currentLogFilter.trim().toLowerCase();
  const dateQuery = currentLogDate.trim();
  const filtered = (query || dateQuery)
    ? allLogEntries.filter(e => {
        const msg = (e.message || '').toLowerCase();
        const lvl = (e.level || '').toLowerCase();
        const formatted = formatTime(e.time);
        const t = `${e.time || ''} ${formatted}`.toLowerCase();
        const matchesQuery = !query || msg.includes(query) || lvl.includes(query) || t.includes(query);
        const matchesDate = !dateQuery || formatted.startsWith(dateQuery);
        return matchesQuery && matchesDate;
      })
    : allLogEntries;

  if (filtered.length === 0) {
    dom.logsList.appendChild(createEmptyStateItem((query || dateQuery) ? '未找到匹配的日志' : '暂无日志记录', 'log'));
    return;
  }

  for (const entry of filtered) {
    const li = document.createElement('li');
    li.className = 'log-entry';

    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-time';
    timeSpan.textContent = formatTime(entry.time);
    li.appendChild(timeSpan);

    const badge = document.createElement('span');
    const isErr = entry.level === 'error';
    badge.className = `log-badge ${isErr ? 'log-badge-error' : 'log-badge-info'}`;
    badge.textContent = isErr ? 'ERR' : 'INF';
    li.appendChild(badge);

    const textSpan = document.createElement('span');
    textSpan.className = 'log-text';
    textSpan.textContent = entry.message || '';
    if (Number.isFinite(entry.duration_seconds)) {
      const duration = document.createElement('span');
      duration.className = entry.active ? 'log-duration is-active' : 'log-duration';
      duration.dataset.index = String(allLogEntries.indexOf(entry));
      duration.setAttribute('aria-live', 'off');
      duration.textContent = ' ' + formatDuration(entry.duration_seconds);
      textSpan.appendChild(duration);
    }
    li.appendChild(textSpan);

    dom.logsList.appendChild(li);
  }
}

function updateLogDurations() {
  const elapsed = Math.max(0, (performance.now() - logsFetchedAt) / 1000);
  dom.logsList.querySelectorAll('.log-duration').forEach(node => {
    const entry = allLogEntries[Number(node.dataset.index)];
    if (!entry) return;
    node.classList.toggle('is-active', Boolean(entry.active));
    const text = ' ' + formatDuration(entry.duration_seconds + (entry.active ? elapsed : 0));
    if (node.textContent !== text) node.textContent = text;
  });
}

function syncLogClock() {
  const active = !document.hidden && !document.getElementById('panel-logs').hidden && allLogEntries.some(entry => entry.active);
  if (active && !logClock) logClock = setInterval(updateLogDurations, 1000);
  if (!active && logClock) { clearInterval(logClock); logClock = null; }
}

async function handleClearLogs() {
  requestConfirm(
    '确认清空日志',
    '确定要清空全部运行日志吗？',
    async () => {
      try {
        await apiRequest('/api/logs/clear', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        showToast('日志已清空', 'success');
        await loadLogs();
      } catch (err) {
        showToast(`清空日志失败: ${err.message}`, 'error');
      }
    }
  );
}

async function handleOpenLogsDir() {
  try {
    await apiRequest('/api/logs/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    showToast('已打开日志文件夹', 'success');
  } catch (err) {
    showToast(`打开失败: ${err.message}`, 'error');
  }
}

// ==========================================================================
// 诊断导出
// ==========================================================================

async function handleExportDiagnostics() {
  if (isBusy) return;
  try {
    if (!sessionToken) await fetchSessionToken();
    showToast('正在导出诊断报告...', 'info');

    const res = await fetch('/api/diagnostics', {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
        'X-NJUST-Token': sessionToken
      },
      cache: 'no-store'
    });

    if (!res.ok) {
      let errMsg = `导出失败 (${res.status})`;
      try {
        const errJson = await res.json();
        if (errJson && errJson.error) errMsg = errJson.error;
      } catch {
        // ignore
      }
      throw new Error(errMsg);
    }

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    a.href = url;
    a.download = `njust-wifi-diagnostics-${ts}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    showToast('诊断报告已下载', 'success');
  } catch (err) {
    showToast(`导出失败: ${err.message}`, 'error');
  }
}

// ==========================================================================
// 主题管理
// ==========================================================================

function initTheme() {
  let theme = null;
  try { theme = localStorage.getItem('njust_glass_theme'); } catch { /* optional */ }
  if (!theme) {
    const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    theme = prefersDark ? 'dark' : 'light';
  }
  applyTheme(theme, false);
}

function applyTheme(theme, save = true) {
  document.documentElement.setAttribute('data-theme', theme);
  if (window.chrome?.webview) window.chrome.webview.postMessage('theme:' + theme);
  if (save) {
    try {
      localStorage.setItem('njust_glass_theme', theme);
    } catch {
      // ignore
    }
  }
  if (dom.themeIconText) {
    dom.themeIconText.textContent = theme === 'dark' ? '浅色模式' : '深色模式';
  }
  if (dom.themeIconSvg) {
    const replacement = createSvgIcon(theme === 'dark' ? 'sun' : 'moon', 16);
    replacement.id = 'theme-icon-svg';
    dom.themeIconSvg.replaceWith(replacement);
    dom.themeIconSvg = replacement;
  }
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  applyTheme(next, true);
}

// ==========================================================================
// 选项卡切换与键盘导航
// ==========================================================================

function switchTab(targetTabId) {
  const title = document.getElementById('page-title');
  if (title) title.textContent = ({'panel-status': '连接状态', 'panel-config': '连接设置', 'panel-probes': '程序守护', 'panel-logs': '运行日志'})[targetTabId] || 'NJUST WiFi';
  dom.tabBtns.forEach(btn => {
    const isMatch = btn.getAttribute('aria-controls') === targetTabId;
    btn.classList.toggle('active', isMatch);
    btn.setAttribute('aria-selected', isMatch ? 'true' : 'false');
    btn.tabIndex = isMatch ? 0 : -1;
  });

  dom.tabPanels.forEach(panel => {
    const isMatch = panel.id === targetTabId;
    panel.classList.toggle('active', isMatch);
    panel.hidden = !isMatch;
  });
  if (targetTabId === 'panel-probes') loadProbes();
  if (targetTabId === 'panel-logs') loadLogs();
  syncLogClock();
}

// ==========================================================================
// 后台轮询与页面休眠 (仅轮询状态、余额、守护、日志，绝不覆盖用户输入)
// ==========================================================================

async function refreshCycle() {
  if (isRefreshing || document.hidden) return;
  isRefreshing = true;
  try {
    const requests = [loadStatus(), loadBalance()];
    proxyUI?.refresh();
    if (!document.getElementById('panel-probes').hidden) requests.push(loadProbes());
    if (!document.getElementById('panel-logs').hidden) requests.push(loadLogs());
    await Promise.all(requests);
  } catch (err) {
    console.warn('轮询部分失败:', err);
  } finally {
    isRefreshing = false;
  }
}

function startPolling() {
  stopPolling();
  if (!document.hidden) pollTimer = setInterval(refreshCycle, 10000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function handleVisibilityChange() {
  syncLogClock();
  if (document.hidden) {
    stopPolling();
  } else {
    refreshCycle();
    startPolling();
  }
}

// ==========================================================================
// 事件绑定与应用启动
// ==========================================================================

function bindEvents() {
  // 主题
  if (dom.btnThemeToggle) dom.btnThemeToggle.addEventListener('click', toggleTheme);

  // 选项卡切换与键盘方向键导航
  dom.tabBtns.forEach(btn => {
    btn.tabIndex = btn.getAttribute('aria-selected') === 'true' ? 0 : -1;
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('aria-controls');
      if (target) switchTab(target);
    });
    btn.addEventListener('keydown', event => {
      const tabs = [...dom.tabBtns];
      let index = tabs.indexOf(btn);
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') index = (index + 1) % tabs.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') index = (index + tabs.length - 1) % tabs.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = tabs.length - 1;
      else return;
      event.preventDefault();
      switchTab(tabs[index].getAttribute('aria-controls'));
      tabs[index].focus();
    });
  });

  // 余额手动刷新
  if (dom.btnRefreshBalance) {
    dom.btnRefreshBalance.addEventListener('click', handleRefreshBalance);
  }

  // 核心控制按钮
  if (dom.btnToggleService) {
    dom.btnToggleService.addEventListener('click', async () => {
      const endpoint = isServiceRunning || isBusy ? '/api/stop' : '/api/start';
      const actionName = isServiceRunning || isBusy ? '暂停监控' : '开始监控';
      try {
        await apiRequest(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        showToast(`已${actionName}`, 'success');
        await loadStatus();
      } catch (err) {
        showToast(`${actionName}失败: ${err.message}`, 'error');
      }
    });
  }

  if (dom.btnCheckNow) {
    dom.btnCheckNow.addEventListener('click', async () => {
      if (isBusy) return;
      try {
        showToast('已发起单次检测...', 'info');
        await apiRequest('/api/check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        showToast('已开始检测', 'info');
        await Promise.all([loadStatus(), loadLogs()]);
      } catch (err) {
        showToast(`检测失败: ${err.message}`, 'error');
      }
    });
  }

  if (dom.btnExportDiag) dom.btnExportDiag.addEventListener('click', handleExportDiagnostics);

  // 备用 WiFi 添加按钮
  if (dom.btnAddFallback) {
    dom.btnAddFallback.addEventListener('click', addFallbackNetwork);
  }

  // 校园网密码显隐按钮
  if (dom.btnToggleCampusPwd) {
    dom.btnToggleCampusPwd.addEventListener('click', () => {
      togglePasswordInput(dom.cfgPassword, dom.btnToggleCampusPwd);
    });
  }

  // 守护程序列表全选
  if (dom.checkAllProbes) {
    dom.checkAllProbes.addEventListener('change', () => {
      if (dom.checkAllProbes.checked) {
        currentProbes.forEach(p => selectedProbeIds.add(p.id));
      } else {
        selectedProbeIds.clear();
      }
      renderProbesList();
    });
  }

  // 守护程序操作按钮
  if (dom.btnOpenAddProbe) dom.btnOpenAddProbe.addEventListener('click', openAddProbeModal);
  if (dom.btnRemoveProbes) dom.btnRemoveProbes.addEventListener('click', handleRemoveSelectedProbes);
  if (dom.btnClearProbes) dom.btnClearProbes.addEventListener('click', handleClearAllProbes);
  if (dom.btnElevateProbes) dom.btnElevateProbes.addEventListener('click', handleElevateSelectedProbes);

  // 添加程序对话框
  if (dom.btnCloseProbeModal) dom.btnCloseProbeModal.addEventListener('click', closeAddProbeModal);
  if (dom.btnCancelAddProbe) dom.btnCancelAddProbe.addEventListener('click', closeAddProbeModal);
  if (dom.btnBrowseFile) dom.btnBrowseFile.addEventListener('click', handleBrowseFile);
  if (dom.btnConfirmAddProbe) dom.btnConfirmAddProbe.addEventListener('click', handleConfirmAddProbe);

  // 参数设置
  if (dom.formConfig) dom.formConfig.addEventListener('submit', event => { event.preventDefault(); autosave?.saveAll(); });


  // 日志筛选与操作
  const syncLogFilterClearBtn = () => {
    if (!dom.btnClearSearch) return;
    if (currentLogFilter || currentLogDate) dom.btnClearSearch.classList.remove('hidden');
    else dom.btnClearSearch.classList.add('hidden');
  };

  if (dom.logSearchInput) {
    dom.logSearchInput.addEventListener('input', () => {
      currentLogFilter = dom.logSearchInput.value;
      syncLogFilterClearBtn();
      renderLogsList();
    });
  }

  if (dom.logDateFilter) {
    dom.logDateFilter.addEventListener('change', () => {
      currentLogDate = dom.logDateFilter.value || '';
      syncLogFilterClearBtn();
      renderLogsList();
    });
  }

  if (dom.btnClearSearch) {
    dom.btnClearSearch.addEventListener('click', () => {
      currentLogFilter = '';
      currentLogDate = '';
      if (dom.logSearchInput) dom.logSearchInput.value = '';
      if (dom.logDateFilter) dom.logDateFilter.value = '';
      syncLogFilterClearBtn();
      renderLogsList();
    });
  }

  if (dom.btnOpenLogsDir) dom.btnOpenLogsDir.addEventListener('click', handleOpenLogsDir);
  if (dom.btnClearLogs) dom.btnClearLogs.addEventListener('click', handleClearLogs);

  // 确认对话框按钮与取消事件
  if (dom.btnConfirmCancel) dom.btnConfirmCancel.addEventListener('click', closeConfirmDialog);
  if (dom.btnConfirmOk) {
    dom.btnConfirmOk.addEventListener('click', () => {
      const cb = pendingConfirmCallback;
      closeConfirmDialog();
      if (typeof cb === 'function') cb();
    });
  }
  if (dom.modalConfirm) {
    dom.modalConfirm.addEventListener('cancel', () => { pendingConfirmCallback = null; });
    dom.modalConfirm.addEventListener('click', (e) => {
      if (e.target === dom.modalConfirm) closeConfirmDialog();
    });
  }
  if (dom.modalAddProbe) {
    dom.modalAddProbe.addEventListener('click', (e) => {
      if (e.target === dom.modalAddProbe) closeAddProbeModal();
    });
  }

  // 页面可见性休眠与唤醒
  document.addEventListener('visibilitychange', handleVisibilityChange);
}

async function initApp() {
  initTheme();
  bindEvents();
  syncControls();

  try {
    await fetchSessionToken();
    await Promise.all([
      loadStatus(),
      loadBalance(),
      loadConfig(),
      loadCredentials(),
      loadProbes(),
      loadLogs()
    ]);
    startPolling();
    autosave = initAutoSave({apiRequest, showToast, ready: () => configLoaded && !mutationPending,
      fallbacks: () => { syncFallbackNetworksFromDOM(); return currentFallbackNetworks.filter(n => n.ssid.trim() || n.password).map(n => ({ssid:n.ssid.trim(), password:n.password})); },
      onSaved: () => { hasSavedCredentials = Boolean(dom.cfgUsername.value && dom.cfgPassword.value); }});
    initWindow({apiRequest, showToast, flush: () => autosave.flush()});
    proxyUI = initProxy({apiRequest, showToast, onClientChanged:loadProbes});
    initUpdates({apiRequest, showToast});
    if (configLoaded) initOnboarding({switchTab, isConfigured: Boolean(hasSavedCredentials || currentFallbackNetworks.length || isServiceRunning), getRunning: () => isServiceRunning});
  } catch (err) {
    console.error('初始化失败:', err);
    showToast(`连接失败: ${err.message}`, 'error', 5000);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}
