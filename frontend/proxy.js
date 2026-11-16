import { initProxyGuide } from './proxy-guide.js';

export function initProxy({ apiRequest, showToast, onClientChanged }) {
  const enabled = document.getElementById('proxy-fail-open');
  const group = document.getElementById('proxy-group');
  const status = document.getElementById('proxy-status');
  const restore = document.getElementById('proxy-restore');
  const detect = document.getElementById('proxy-detect');
  const check = document.getElementById('proxy-check');
  const egress = document.getElementById('proxy-egress');
  const client = document.getElementById('proxy-client');

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

  function scrollControllerIntoView() {
    if (!controllerDetails) return;
    const reduced =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    controllerDetails.scrollIntoView({ block: 'nearest', behavior: reduced ? 'auto' : 'smooth' });
  }

  function renderControllerMeta() {
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
          syncedClient = nextClient;
          if (!urlDirty && !secretDirty) draftClient = nextClient;

          client.value = nextClient;
          enabled.checked = data.enabled;
          status.textContent = data.message || '未启用自动切换';
          const egressText = data.egress ? `当前出口：${data.egress}` : '';
          egress.textContent = egressText;
          egress.hidden = !egressText;
          restore.disabled = !data.restore_available;
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
      showToast(selected ? '已启用代理故障自动切换' : '已关闭自动切换', 'success');
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      enabled.disabled = false;
      refresh();
    }
  });

  async function switchClient(nextClient) {
    if (pendingClientSwitch || switchingClient) return false;
    if (nextClient === syncedClient) return true;
    client.disabled = true;
    pendingClientSwitch = true;
    try {
      if (!(await waitPendingControllerSave())) {
        client.value = syncedClient;
        return false;
      }
      switchingClient = true;
      renderControllerMeta();
      clearDraftsAndFields(nextClient);
      syncedClient = nextClient;
      refreshEpoch += 1;
      await post('/api/proxy/client', { client: nextClient });
      group.replaceChildren(new Option('未选择', ''));
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
      client.disabled = false;
      renderControllerMeta();
      await refresh();
    }
  }
  client.addEventListener('change', () => switchClient(client.value));

  check.addEventListener('click', async () => {
    check.disabled = true;
    try {
      if (!(await waitPendingControllerSave())) return;
      await post('/api/proxy/check', {});
      showToast(enabled.checked ? '正在检测当前代理出口' : '正在检测出口；自动关闭尚未开启', 'info');
    } catch (error) {
      showToast(error.message, 'error');
    } finally {
      check.disabled = false;
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
      showToast('代理客户端已连接', 'success');
    } catch (error) {
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
  refresh();
  return { refresh };
}
