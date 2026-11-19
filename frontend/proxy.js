import { initProxyGuide } from './proxy-guide.js';
import { initProxyControls } from './proxy-controls.js';

export function initProxy({ apiRequest, showToast, onClientChanged }) {
  const enabled = document.getElementById('proxy-fail-open');
  const group = document.getElementById('proxy-group');
  const status = document.getElementById('proxy-status');
  const restore = document.getElementById('proxy-restore');
  const detect = document.getElementById('proxy-detect');
  const check = document.getElementById('proxy-check');
  const client = document.getElementById('proxy-client');
  const stateBox = document.getElementById('proxy-state');
  const stateChip = document.getElementById('proxy-state-chip');
  const checked = document.getElementById('proxy-checked');
  const detail = document.getElementById('proxy-detail');
  const connection = document.getElementById('proxy-connection');
  const autoRestore = document.getElementById('proxy-auto-restore');
  const clearStale = document.getElementById('proxy-clear-stale');
  const summary = document.getElementById('summary-proxy');
  const checkToast = document.getElementById('proxy-check-toast');
  const nodesModal = document.getElementById('modal-proxy-nodes');
  const nodesOpen = document.getElementById('proxy-nodes-open');
  const nodeGroup = document.getElementById('proxy-node-group');
  const nodeSearch = document.getElementById('proxy-node-search');
  const nodeList = document.getElementById('proxy-node-list');
  const nodesState = document.getElementById('proxy-nodes-state');
  const nodesRefresh = document.getElementById('proxy-nodes-refresh');
  let nodesData = null;
  let nodesEpoch = 0;
  let nodeSaving = false;
  let checkEpoch = 0;
  let checkDismissed = false;
  let checkTimer = null;
  let proxyReady = false;

  const controllerDetails = document.getElementById('proxy-controller-details');
  const controllerMode = document.getElementById('proxy-controller-mode');
  const controllerUrl = document.getElementById('proxy-controller-url');
  const controllerSecret = document.getElementById('proxy-controller-secret');
  const controllerReset = document.getElementById('proxy-controller-reset');

  let activeRefresh = null;
  let queuedRefresh = false;
  let controllerFetchPromise = null;
  let controllerFetchEpoch = -1;
  let refreshEpoch = 0;

  let syncedClient = client.value || 'clash-verge';
  let draftClient = syncedClient;
  let lastSavedUrl = '';
  let urlOverride = '';
  let lastSavedSecret = '';
  let secretOverride = false;
  let controllerAutomatic = true;
  let urlDirty = false;
  let secretDirty = false;
  let urlDraftVersion = 0;
  let secretDraftVersion = 0;
  let controllerSaving = false;
  let controllerSavePromise = null;
  let pendingClientSwitch = false;
  let switchingClient = false;
  let resetPointerDown = false;

  const post = (path, payload) =>
    apiRequest(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

  const controls = initProxyControls({
    apiRequest,
    showToast,
    getClient: () => syncedClient,
    onChanged: () => refresh()
  });

  // 后端状态 → 徽标文字与配色（配色沿用网络状态的 state-* 类）
  const STATES = {
    healthy: ['出口正常', 'state-online'],
    restored: ['已恢复', 'state-online'],
    checking: ['复核中', 'state-checking'],
    direct: ['已关闭代理', 'state-recovering'],
    uncertain: ['待确认', 'state-idle'],
    failed: ['出口异常', 'state-error'],
    error: ['需要处理', 'state-error']
  };

  function clock(iso) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    return [date.getHours(), date.getMinutes(), date.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
  }

  function renderConnection(connected) {
    if (!connection) return;
    connection.hidden = typeof connected !== 'boolean';
    connection.textContent = connected ? '已连接' : '未连接';
    connection.className = connected ? 'chip chip-soft' : 'chip chip-muted';
  }

  function renderState(data) {
    const watching = data.enabled || data.clear_stale || data.restore_available;
    const [label, tone] = STATES[data.exit_state || data.state] || [watching ? '待检测' : '未启用', 'state-idle'];
    if (stateBox) stateBox.className = `proxy-state ${tone}`;
    if (stateChip) {
      stateChip.textContent = label;
      stateChip.className = `status-chip ${tone}`;
    }
    if (summary) {
      summary.textContent = label;
      summary.className = tone;
    }
    if (checked) {
      const time = clock(data.checked_at);
      checked.textContent = time ? `上次检测 ${time}` : '';
      checked.title = data.checked_at ? new Date(data.checked_at).toLocaleString() : '';
    }
    if (detail) {
      const parts = [];
      const closed = Array.isArray(data.closed) ? data.closed : [];
      if (closed.length) {
        const names = closed.map(key => (key === 'tun' ? 'TUN' : '系统代理')).join('、');
        const time = clock(data.closed_at);
        parts.push(`已关闭 ${names}${time ? `（${time}）` : ''}`);
        if (data.recovering) parts.push(`恢复确认 ${data.recovering.count}/${data.recovering.needed}`);
        else if (data.auto_restore_paused) parts.push('自动恢复已停止');
      }
      detail.textContent = parts.join(' · ');
      detail.hidden = !parts.length;
    }
    renderConnection(data.connected);
  }

  function fillGroups(names, placeholder) {
    const selected = group.value;
    group.replaceChildren(new Option(placeholder, ''));
    for (const name of names) group.add(new Option(name, name));
    if (selected && !names.includes(selected)) group.add(new Option(selected, selected));
    group.value = selected;
  }

  // 打开守护页时静默连接一次：读到代理组就填好，读不到也不打扰
  let connecting = null;
  function connect() {
    if (connecting || switchingClient || pendingClientSwitch || document.activeElement === group) return connecting;
    connecting = apiRequest('/api/proxy/connect')
      .then(data => {
        if (switchingClient || pendingClientSwitch) return;
        renderConnection(Boolean(data.connected));
        if (data.connected && Array.isArray(data.groups)) fillGroups(data.groups, '请选择代理组');
        controls.refreshLive({ force: true });
      })
      .catch(() => {})
      .finally(() => { connecting = null; });
    return connecting;
  }

  function hideCheckToast() {
    clearTimeout(checkTimer);
    if (checkToast.matches(':popover-open')) checkToast.hidePopover();
    checkToast.hidden = true;
  }

  function cancelCheckFeedback() {
    checkEpoch += 1;
    checkDismissed = true;
    hideCheckToast();
    check.disabled = false;
  }

  function showCheckToast(state, title, message = '') {
    if (checkDismissed) return;
    clearTimeout(checkTimer);
    checkToast.dataset.state = state;
    document.getElementById('proxy-check-title').textContent = title;
    document.getElementById('proxy-check-message').textContent = message;
    document.getElementById('proxy-check-symbol').setAttribute('d', state === 'healthy' ? 'm6 12 4 4 8-8' : state === 'failed' ? 'm7 7 10 10M17 7 7 17' : 'M12 7v6m0 4h.01');
    checkToast.hidden = false;
    if (typeof checkToast.showPopover === 'function') {
      if (!checkToast.matches(':popover-open')) checkToast.showPopover();
    } else checkToast.removeAttribute('popover');
    if (state !== 'loading') checkTimer = setTimeout(hideCheckToast, 6000);
  }

  document.getElementById('proxy-check-dismiss').addEventListener('click', () => {
    checkDismissed = true;
    hideCheckToast();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && nodesModal.open) {
      event.preventDefault();
      nodesModal.close();
      return;
    }
    if (event.key === 'Escape' && !nodesModal.open && !checkToast.hidden) {
      checkDismissed = true;
      hideCheckToast();
    }
  });

  function renderNodes() {
    const current = nodesData?.groups.find(item => item.name === nodeGroup.value);
    nodeList.replaceChildren();
    if (!current) return;
    const query = nodeSearch.value.trim().toLocaleLowerCase();
    const names = current.all.filter(name => name.toLocaleLowerCase().includes(query));
    nodesState.textContent = current.selectable ? `${names.length} 个节点` : '自动选择';
    const fragment = document.createDocumentFragment();
    for (const name of names) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'proxy-node-option';
      button.setAttribute('aria-pressed', String(name === current.selected));
      button.disabled = !current.selectable || nodeSaving;
      const marker = document.createElement('span');
      marker.className = 'proxy-node-check';
      marker.setAttribute('aria-hidden', 'true');
      marker.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="m6 12 4 4 8-8"/></svg>';
      const label = document.createElement('span');
      label.className = 'proxy-node-name'; label.textContent = name; label.title = name;
      const meta = document.createElement('span'); meta.className = 'proxy-node-meta';
      const data = nodesData.nodes[name];
      meta.textContent = nodesData.groups.some(g => g.name === name) ? '代理组' : data?.delay ? `${data.delay} ms` : '';
      button.append(marker, label, meta);
      button.addEventListener('click', () => chooseNode(current.name, name));
      item.append(button); fragment.append(item);
    }
    if (!names.length) {
      const empty = document.createElement('li'); empty.className = 'item-empty'; empty.textContent = '没有匹配的节点'; fragment.append(empty);
    }
    nodeList.append(fragment);
  }

  async function loadNodes() {
    const epoch = ++nodesEpoch;
    const targetClient = syncedClient;
    nodesRefresh.disabled = nodeGroup.disabled = nodeSearch.disabled = true;
    nodeList.setAttribute('aria-busy', 'true');
    nodesState.textContent = '正在读取…';
    for (const button of nodeList.querySelectorAll('button')) button.disabled = true;
    try {
      if (!(await waitPendingControllerSave())) return;
      const data = await apiRequest('/api/proxy/nodes');
      if (epoch !== nodesEpoch || !nodesModal.open) return;
      if (targetClient !== syncedClient || data.client !== targetClient) throw new Error('客户端已变化，请刷新节点列表');
      const preferred = nodeGroup.value || group.value;
      nodesData = data;
      nodeGroup.replaceChildren();
      for (const row of data.groups) nodeGroup.add(new Option(row.name, row.name));
      if (data.groups.some(row => row.name === preferred)) nodeGroup.value = preferred;
      else nodeGroup.value = (data.groups.find(row => row.selectable) || data.groups[0])?.name || '';
      nodeGroup.disabled = nodeSearch.disabled = data.groups.length === 0;
      if (!data.groups.length) {
        nodeGroup.add(new Option('暂无代理组', ''));
        nodeList.replaceChildren(); nodesState.textContent = '暂无代理组';
      } else renderNodes();
    } catch (error) {
      if (epoch === nodesEpoch && nodesModal.open) {
        nodesData = null;
        nodeList.replaceChildren(); nodeGroup.replaceChildren(new Option('未连接', ''));
        nodesState.textContent = error.message;
      }
    } finally {
      if (epoch === nodesEpoch) {
        nodesRefresh.disabled = false;
        nodeList.setAttribute('aria-busy', 'false');
      }
    }
  }

  async function chooseNode(name, node) {
    if (nodeSaving || !nodesData || nodesData.client !== syncedClient) return;
    const selected = nodesData.groups.find(item => item.name === name);
    if (!selected?.selectable || selected.selected === node) return;
    const targetClient = nodesData.client;
    nodeSaving = true;
    nodeGroup.disabled = nodeSearch.disabled = nodesRefresh.disabled = true;
    nodesState.textContent = '正在切换…';
    for (const button of nodeList.querySelectorAll('button')) button.disabled = true;
    try {
      await post('/api/proxy/node', {client: targetClient, group: name, node});
      if (targetClient !== syncedClient) return;
      cancelCheckFeedback();
      showToast(`已切换至 ${node}`, 'success');
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      nodeSaving = false;
      if (nodesModal.open && targetClient === syncedClient) await loadNodes();
      refresh();
    }
  }

  nodesOpen.addEventListener('click', () => {
    nodeSearch.value = '';
    nodesModal.showModal();
    loadNodes();
  });
  document.getElementById('proxy-nodes-close').addEventListener('click', () => nodesModal.close());
  nodesModal.addEventListener('click', event => { if (event.target === nodesModal) nodesModal.close(); });
  nodesModal.addEventListener('close', () => {
    nodesEpoch += 1;
    nodesData = null;
    nodeList.replaceChildren();
    nodesOpen.focus({preventScroll: true});
  });
  nodeGroup.addEventListener('change', () => { nodeSearch.value = ''; renderNodes(); });
  nodeSearch.addEventListener('input', renderNodes);
  nodesRefresh.addEventListener('click', loadNodes);

  function scrollControllerIntoView() {
    if (!controllerDetails) return;
    const reduced =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    controllerDetails.scrollIntoView({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' });
  }

  function renderControllerMeta() {
    nodesOpen.disabled = !proxyReady || client.disabled || switchingClient || pendingClientSwitch;
    const isFlClash = syncedClient === 'flclash';
    const manual = Boolean(urlOverride) || Boolean(secretOverride) || !controllerAutomatic;
    if (controllerMode) {
      controllerMode.textContent = manual ? '手动' : '自动';
      controllerMode.className = manual ? 'chip chip-soft' : 'chip chip-muted';
    }
    if (controllerUrl) {
      controllerUrl.placeholder = isFlClash ? 'http://127.0.0.1:9090' : 'http://127.0.0.1:9097';
      controllerUrl.disabled = switchingClient;
    }
    if (controllerSecret) {
      controllerSecret.disabled = switchingClient;
    }
    if (controllerReset) {
      controllerReset.disabled = controllerSaving || switchingClient;
    }
  }

  function runControllerMutation(task) {
    const prev = controllerSavePromise || Promise.resolve(true);
    const next = prev.catch(() => false).then(() => task());
    let settled;
    settled = next.finally(() => {
      if (controllerSavePromise === settled) controllerSavePromise = null;
    });
    controllerSavePromise = settled;
    return settled;
  }

  async function waitPendingControllerSave() {
    if (!controllerSavePromise) return true;
    try {
      return await controllerSavePromise;
    } catch {
      return false;
    }
  }

  function fetchControllerOnce(epoch = refreshEpoch) {
    if (controllerFetchPromise && controllerFetchEpoch === epoch) {
      return controllerFetchPromise;
    }
    controllerFetchEpoch = epoch;
    const p = apiRequest('/api/proxy/controller')
      .then(
        data => ({ ok: true, data }),
        () => ({ ok: false, data: null })
      )
      .finally(() => {
        if (controllerFetchPromise === p) controllerFetchPromise = null;
      });
    controllerFetchPromise = p;
    return p;
  }

  function clearDraftsAndFields(nextClient) {
    urlDirty = false;
    secretDirty = false;
    urlDraftVersion += 1;
    secretDraftVersion += 1;
    lastSavedUrl = '';
    urlOverride = '';
    lastSavedSecret = '';
    secretOverride = false;
    controllerAutomatic = true;
    draftClient = nextClient;
    if (controllerUrl) {
      controllerUrl.value = '';
      controllerUrl.removeAttribute('aria-invalid');
    }
    if (controllerSecret) {
      controllerSecret.value = '';
      controllerSecret.removeAttribute('aria-invalid');
    }
  }

  function applyControllerDetails(ctrl, clientChangedOnServer) {
    if (!ctrl || (ctrl.client && ctrl.client !== syncedClient)) return false;

    lastSavedUrl = typeof ctrl.url === 'string' ? ctrl.url : '';
    urlOverride = typeof ctrl.url_override === 'string' ? ctrl.url_override : '';
    lastSavedSecret = typeof ctrl.secret === 'string' ? ctrl.secret : '';
    secretOverride = Boolean(ctrl.secret_override);
    controllerAutomatic = ctrl.automatic !== undefined ? Boolean(ctrl.automatic) : (!urlOverride && !secretOverride);

    if (clientChangedOnServer && !controllerSaving) {
      urlDirty = false;
      secretDirty = false;
      urlDraftVersion += 1;
      secretDraftVersion += 1;
      draftClient = syncedClient;
      if (controllerUrl) {
        controllerUrl.value = lastSavedUrl;
        controllerUrl.removeAttribute('aria-invalid');
      }
      if (controllerSecret) {
        controllerSecret.value = lastSavedSecret;
        controllerSecret.removeAttribute('aria-invalid');
      }
    } else {
      if (controllerUrl && !urlDirty && !controllerSaving && document.activeElement !== controllerUrl) {
        controllerUrl.value = lastSavedUrl;
        controllerUrl.removeAttribute('aria-invalid');
      }
      if (controllerSecret && !secretDirty && !controllerSaving && document.activeElement !== controllerSecret) {
        controllerSecret.value = lastSavedSecret;
        controllerSecret.removeAttribute('aria-invalid');
      }
    }
    renderControllerMeta();
    return true;
  }

  function applyFallbackSummary(summary, clientChangedOnServer) {
    const ctrl = summary || {};
    lastSavedUrl = typeof ctrl.url === 'string' ? ctrl.url : '';
    urlOverride = lastSavedUrl;
    secretOverride = Boolean(ctrl.secret_set);
    controllerAutomatic = ctrl.automatic !== undefined ? Boolean(ctrl.automatic) : (!urlOverride && !secretOverride);
    if (clientChangedOnServer) {
      lastSavedSecret = '';
      urlDirty = false;
      secretDirty = false;
      urlDraftVersion += 1;
      secretDraftVersion += 1;
      draftClient = syncedClient;
      if (controllerUrl) {
        controllerUrl.value = lastSavedUrl;
        controllerUrl.removeAttribute('aria-invalid');
      }
      if (controllerSecret) {
        controllerSecret.value = '';
        controllerSecret.removeAttribute('aria-invalid');
      }
    } else if (controllerUrl && !urlDirty && !controllerSaving && document.activeElement !== controllerUrl) {
      controllerUrl.value = lastSavedUrl;
      controllerUrl.removeAttribute('aria-invalid');
    }
    renderControllerMeta();
  }

  function refresh() {
    if (activeRefresh) {
      queuedRefresh = true;
      return activeRefresh;
    }
    activeRefresh = (async () => {
      do {
        queuedRefresh = false;
        const epoch = refreshEpoch;
        try {
          const [data, initialCtrl] = await Promise.all([
            apiRequest('/api/proxy'),
            fetchControllerOnce(epoch)
          ]);
          if (epoch !== refreshEpoch || switchingClient || pendingClientSwitch) continue;

          const nextClient = data.client || 'clash-verge';
          const clientChangedOnServer = nextClient !== syncedClient;
          if (clientChangedOnServer) {
            cancelCheckFeedback();
            if (nodesModal.open) nodesModal.close();
            nodesData = null;
          }
          syncedClient = nextClient;
          if (!urlDirty && !secretDirty) draftClient = nextClient;

          client.value = nextClient;
          proxyReady = true;
          enabled.checked = data.enabled;
          if (autoRestore && !autoRestore.disabled) autoRestore.checked = Boolean(data.auto_restore);
          if (clearStale && !clearStale.disabled) clearStale.checked = Boolean(data.clear_stale);
          status.textContent = data.message || '未启用自动处置';
          renderState(data);
          controls.render(data);
          controls.refreshLive();
          restore.disabled = !data.restore_available;
          restore.classList.toggle('btn-primary', Boolean(data.restore_available));
          if (data.group && ![...group.options].some(option => option.value === data.group)) {
            group.add(new Option(data.group, data.group));
          }
          group.value = data.group;

          if (clientChangedOnServer && !controllerSaving) {
            clearDraftsAndFields(nextClient);
          }

          let ctrlResult = initialCtrl;
          if (ctrlResult?.ok && ctrlResult.data?.client && ctrlResult.data.client !== syncedClient) {
            controllerFetchEpoch = -1;
            controllerFetchPromise = null;
            ctrlResult = await fetchControllerOnce(epoch);
            if (epoch !== refreshEpoch || switchingClient || pendingClientSwitch) continue;
          }

          if (ctrlResult?.ok && (!ctrlResult.data?.client || ctrlResult.data.client === syncedClient)) {
            applyControllerDetails(ctrlResult.data, clientChangedOnServer);
          } else {
            applyFallbackSummary(data.controller, clientChangedOnServer);
          }
        } catch {
          if (epoch === refreshEpoch && !switchingClient) {
            status.textContent = '服务待连接';
            if (summary) {
              summary.textContent = '—';
              summary.className = '';
            }
          }
        }
      } while (queuedRefresh);
    })().finally(() => {
      activeRefresh = null;
    });
    return activeRefresh;
  }

  async function refreshControllerOnOpen() {
    if (switchingClient) return;
    if (activeRefresh) {
      await activeRefresh;
      return;
    }
    const epoch = refreshEpoch;
    const expectedClient = syncedClient;
    const result = await fetchControllerOnce(epoch);
    if (!result?.ok || epoch !== refreshEpoch || switchingClient) return;
    if (result.data?.client && result.data.client !== syncedClient) return;
    if (expectedClient && syncedClient !== expectedClient) return;
    applyControllerDetails(result.data, false);
  }

  function saveUrlDraft() {
    if (!controllerUrl || switchingClient) return Promise.resolve(true);
    const targetClient = draftClient;
    const nextUrl = controllerUrl.value.trim();
    if (targetClient !== syncedClient) return Promise.resolve(true);
    if (nextUrl === lastSavedUrl) {
      urlDirty = false;
      controllerUrl.value = lastSavedUrl;
      controllerUrl.removeAttribute('aria-invalid');
      return Promise.resolve(true);
    }
    return runControllerMutation(async () => {
      if (targetClient !== syncedClient || switchingClient) return true;
      const submittedUrl = controllerUrl.value.trim();
      if (submittedUrl === lastSavedUrl) {
        urlDirty = false;
        controllerUrl.value = lastSavedUrl;
        controllerUrl.removeAttribute('aria-invalid');
        return true;
      }
      controllerSaving = true;
      const savedVersion = urlDraftVersion;
      renderControllerMeta();
      refreshEpoch += 1;
      try {
        await post('/api/proxy/controller', { client: targetClient, url: submittedUrl });
        if (targetClient === syncedClient && urlDraftVersion === savedVersion) {
          urlDirty = false;
          controllerUrl.removeAttribute('aria-invalid');
        }
        showToast(submittedUrl ? '外部控制器 API 已保存' : '已恢复自动读取 API', 'success');
        return true;
      } catch (error) {
        if (targetClient === syncedClient) {
          urlDirty = true;
          controllerUrl.setAttribute('aria-invalid', 'true');
        }
        showToast(error.message, 'error');
        return false;
      } finally {
        controllerSaving = false;
        renderControllerMeta();
        if (!pendingClientSwitch) await refresh();
      }
    });
  }

  function saveSecretDraft() {
    if (!controllerSecret || switchingClient) return Promise.resolve(true);
    const targetClient = draftClient;
    const nextSecret = controllerSecret.value;
    if (targetClient !== syncedClient) return Promise.resolve(true);
    if (nextSecret === '') {
      secretDirty = false;
      controllerSecret.removeAttribute('aria-invalid');
      controllerSecret.value = lastSavedSecret;
      return Promise.resolve(true);
    }
    if (nextSecret === lastSavedSecret) {
      secretDirty = false;
      controllerSecret.removeAttribute('aria-invalid');
      return Promise.resolve(true);
    }
    return runControllerMutation(async () => {
      if (targetClient !== syncedClient || switchingClient) return true;
      const submittedSecret = controllerSecret.value;
      if (submittedSecret === '') {
        secretDirty = false;
        controllerSecret.removeAttribute('aria-invalid');
        controllerSecret.value = lastSavedSecret;
        return true;
      }
      if (submittedSecret === lastSavedSecret) {
        secretDirty = false;
        controllerSecret.removeAttribute('aria-invalid');
        return true;
      }
      controllerSaving = true;
      const savedVersion = secretDraftVersion;
      renderControllerMeta();
      refreshEpoch += 1;
      try {
        await post('/api/proxy/controller', {
          client: targetClient,
          url: urlOverride,
          secret: submittedSecret
        });
        if (targetClient === syncedClient && secretDraftVersion === savedVersion) {
          secretDirty = false;
          controllerSecret.removeAttribute('aria-invalid');
        }
        showToast('外部控制器密钥已保存', 'success');
        return true;
      } catch (error) {
        if (targetClient === syncedClient) {
          secretDirty = true;
          controllerSecret.setAttribute('aria-invalid', 'true');
        }
        showToast(error.message, 'error');
        return false;
      } finally {
        controllerSaving = false;
        renderControllerMeta();
        if (!pendingClientSwitch) await refresh();
      }
    });
  }

  function resetToAutomatic() {
    resetPointerDown = false;
    if (!controllerReset || switchingClient) return Promise.resolve(true);
    const targetClient = syncedClient;
    return runControllerMutation(async () => {
      if (targetClient !== syncedClient || switchingClient) return true;
      controllerSaving = true;
      urlDraftVersion += 1;
      secretDraftVersion += 1;
      const savedUrlVer = urlDraftVersion;
      const savedSecretVer = secretDraftVersion;
      renderControllerMeta();
      refreshEpoch += 1;
      try {
        await post('/api/proxy/controller', {
          client: targetClient,
          url: '',
          clear_secret: true
        });
        if (targetClient === syncedClient) {
          if (urlDraftVersion === savedUrlVer) {
            urlDirty = false;
            controllerUrl?.removeAttribute('aria-invalid');
          }
          if (secretDraftVersion === savedSecretVer) {
            secretDirty = false;
            controllerSecret?.removeAttribute('aria-invalid');
          }
        }
        showToast('已恢复自动读取', 'success');
        return true;
      } catch (error) {
        showToast(error.message, 'error');
        return false;
      } finally {
        controllerSaving = false;
        renderControllerMeta();
        if (!pendingClientSwitch) await refresh();
      }
    });
  }

  initProxyGuide({
    getClient: () => syncedClient || client.value || 'clash-verge',
    onConfigure: async guideClient => {
      if (!controllerDetails) return;
      if (guideClient && guideClient !== syncedClient) {
        client.value = guideClient;
        if (!(await switchClient(guideClient))) return;
      }
      const wasOpen = controllerDetails.open;
      if (!wasOpen) {
        controllerDetails.open = true;
      } else {
        refreshControllerOnOpen();
      }
      scrollControllerIntoView();
      controllerUrl?.focus({ preventScroll: true });
    }
  });

  if (controllerUrl) {
    controllerUrl.addEventListener('focus', () => {
      if (!urlDirty) draftClient = syncedClient;
    });
    controllerUrl.addEventListener('input', () => {
      draftClient = syncedClient;
      urlDraftVersion += 1;
      urlDirty = controllerSaving || controllerUrl.value.trim() !== lastSavedUrl;
      controllerUrl.removeAttribute('aria-invalid');
    });
    controllerUrl.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        controllerUrl.blur();
      }
    });
    controllerUrl.addEventListener('blur', event => {
      if (event.relatedTarget === controllerReset || resetPointerDown) return;
      if (urlDirty || controllerUrl.value.trim() !== lastSavedUrl) {
        if (!urlDirty) draftClient = syncedClient;
        saveUrlDraft();
      }
    });
  }

  if (controllerSecret) {
    controllerSecret.addEventListener('focus', () => {
      if (!secretDirty) draftClient = syncedClient;
    });
    controllerSecret.addEventListener('input', () => {
      draftClient = syncedClient;
      secretDraftVersion += 1;
      secretDirty = controllerSaving || controllerSecret.value !== lastSavedSecret;
      controllerSecret.removeAttribute('aria-invalid');
    });
    controllerSecret.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        controllerSecret.blur();
      }
    });
    controllerSecret.addEventListener('blur', event => {
      if (event.relatedTarget === controllerReset || resetPointerDown) return;
      if (controllerSecret.value === '') {
        secretDirty = false;
        controllerSecret.removeAttribute('aria-invalid');
        controllerSecret.value = lastSavedSecret;
        return;
      }
      if (secretDirty || controllerSecret.value !== lastSavedSecret) {
        if (!secretDirty) draftClient = syncedClient;
        saveSecretDraft();
      }
    });
  }

  if (controllerReset) {
    controllerReset.addEventListener('pointerdown', () => {
      if (document.activeElement === controllerUrl || document.activeElement === controllerSecret) {
        resetPointerDown = true;
        window.addEventListener('pointerup', () => setTimeout(() => { resetPointerDown = false; }, 0), { once: true });
      }
    });
    controllerReset.addEventListener('click', resetToAutomatic);
  }

  if (controllerDetails) {
    controllerDetails.addEventListener('toggle', () => {
      if (controllerDetails.open) {
        scrollControllerIntoView();
        refreshControllerOnOpen();
      } else if (!controllerSavePromise) {
        if (urlDirty && !controllerUrl?.hasAttribute('aria-invalid')) saveUrlDraft();
        if (secretDirty && !controllerSecret?.hasAttribute('aria-invalid')) saveSecretDraft();
      }
    });
  }

  enabled.addEventListener('change', async () => {
    const selected = enabled.checked;
    enabled.disabled = true;
    try {
      await waitPendingControllerSave();
      await post('/api/config', { proxy_fail_open: selected });
      showToast(selected ? '已开启自动处置，出口异常时按顺序执行' : '已关闭自动处置，只检测不改动', 'success');
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      enabled.disabled = false;
      refresh();
    }
  });

  for (const [input, key, on, off] of [
    [autoRestore, 'auto_restore', '恢复正常后将自动恢复代理', '已改为手动恢复'],
    [clearStale, 'clear_stale', '已开启残留系统代理清理', '已关闭残留系统代理清理']
  ]) {
    input?.addEventListener('change', async () => {
      const selected = input.checked;
      input.disabled = true;
      try {
        await post('/api/proxy/options', { [key]: selected });
        showToast(selected ? on : off, 'success');
      } catch (error) {
        input.checked = !selected;
        showToast(error.message, 'error');
      } finally {
        input.disabled = false;
        refresh();
      }
    });
  }

  async function switchClient(nextClient) {
    if (pendingClientSwitch || switchingClient) return false;
    if (nextClient === syncedClient) return true;
    client.disabled = true;
    nodesOpen.disabled = true;
    pendingClientSwitch = true;
    try {
      if (!(await waitPendingControllerSave())) {
        client.value = syncedClient;
        return false;
      }
      switchingClient = true;
      cancelCheckFeedback();
      if (nodesModal.open) nodesModal.close();
      nodesData = null;
      renderControllerMeta();
      clearDraftsAndFields(nextClient);
      syncedClient = nextClient;
      controls.reset();
      refreshEpoch += 1;
      await post('/api/proxy/client', { client: nextClient });
      group.replaceChildren(new Option('未选择', ''));
      renderConnection(null);
      await onClientChanged?.();
      showToast('已切换代理客户端', 'success');
      return true;
    } catch (error) {
      syncedClient = '';
      showToast(error.message, 'error');
      return false;
    } finally {
      pendingClientSwitch = false;
      switchingClient = false;
      await refresh();
      client.disabled = false;
      renderControllerMeta();
      connect();
    }
  }
  client.addEventListener('change', () => switchClient(client.value));

  check.addEventListener('click', async () => {
    const epoch = ++checkEpoch;
    let submissionLocked = true;
    const releaseSubmission = () => {
      if (!submissionLocked) return;
      submissionLocked = false;
      if (epoch === checkEpoch && !pendingClientSwitch && !switchingClient) client.disabled = false;
      renderControllerMeta();
    };
    checkDismissed = false;
    check.disabled = true;
    client.disabled = true;
    nodesOpen.disabled = true;
    showCheckToast('loading', '正在检测代理出口');
    try {
      if (!(await waitPendingControllerSave())) return;
      const targetClient = syncedClient;
      const submitted = await post('/api/proxy/check', {});
      releaseSubmission();
      if (!submitted.check_id) throw new Error('请重新打开软件后重试检测');
      const deadline = Date.now() + 90000;
      while (Date.now() < deadline) {
        if (epoch !== checkEpoch || targetClient !== syncedClient || switchingClient) return;
        const job = await apiRequest('/api/proxy/check');
        if (epoch !== checkEpoch || targetClient !== syncedClient) return;
        if (job.id !== submitted.check_id || job.client !== targetClient || job.state === 'cancelled') {
          throw new Error('检测已变更，请重新检测');
        }
        if (job.state === 'done' && job.result) {
          const result = job.result;
          const state = ['healthy', 'failed'].includes(result.exit_state) ? result.exit_state : 'uncertain';
          const titles = {healthy: '代理出口正常', failed: '代理出口异常', uncertain: '出口待确认'};
          const lines = [];
          if (result.egress) lines.push(result.exit_delay > 0 ? `${result.egress} · ${Math.round(result.exit_delay)} ms` : result.egress);
          if (result.closed?.length) lines.push('已关闭' + result.closed.map(key => key === 'tun' ? ' TUN' : '系统代理').join('、'));
          else if (state !== 'healthy') lines.push(result.message || '请检查客户端的外部控制器');
          showCheckToast(state, titles[state], lines.join('\n'));
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 700));
      }
      throw new Error('检测用时较长，请稍后重试');
    } catch (error) {
      if (epoch === checkEpoch) showCheckToast('failed', '检测未完成', error.message);
    } finally {
      releaseSubmission();
      if (epoch === checkEpoch) {
        check.disabled = false;
        if (!checkToast.hidden && checkToast.dataset.state === 'loading') hideCheckToast();
      }
      refresh();
    }
  });

  detect.addEventListener('click', async () => {
    detect.disabled = true;
    try {
      if (!(await waitPendingControllerSave())) return;
      const selected = group.value;
      const data = await apiRequest('/api/proxy/groups');
      group.replaceChildren(new Option('请选择代理组', ''));
      for (const name of data.groups) group.add(new Option(name, name));
      group.value = selected;
      renderConnection(true);
      showToast('代理客户端已连接', 'success');
    } catch (error) {
      renderConnection(false);
      showToast(error.message, 'error');
    } finally {
      detect.disabled = false;
    }
  });

  group.addEventListener('change', async () => {
    group.disabled = true;
    try {
      if (!(await waitPendingControllerSave())) return;
      await post('/api/proxy/group', { group: group.value });
      showToast('代理组已设置', 'success');
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      group.disabled = false;
      refresh();
    }
  });

  restore.addEventListener('click', async () => {
    restore.disabled = true;
    try {
      await waitPendingControllerSave();
      await post('/api/proxy/restore', {});
      showToast('已恢复代理设置', 'success');
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      refresh();
    }
  });

  renderControllerMeta();
  refresh().then(connect);
  return { refresh, connect };
}
