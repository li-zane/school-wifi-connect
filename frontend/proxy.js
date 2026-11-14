import { initProxyGuide } from './proxy-guide.js';

export function initProxy({ apiRequest, showToast, onClientChanged, togglePasswordInput }) {
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
  const controllerSecretToggle = document.getElementById('proxy-controller-secret-toggle');
  const controllerSecretState = document.getElementById('proxy-controller-secret-state');
  const controllerClearSecret = document.getElementById('proxy-controller-clear-secret');

  let activeRefresh = null;
  let queuedRefresh = false;
  let refreshEpoch = 0;

  let syncedClient = client.value || 'clash-verge';
  let draftClient = syncedClient;
  let lastSavedUrl = '';
  let secretSet = false;
  let urlDirty = false;
  let secretDirty = false;
  let controllerSaving = false;
  let controllerSavePromise = null;
  let switchingClient = false;

  const post = (path, payload) =>
    apiRequest(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

  initProxyGuide({ getClient: () => syncedClient || client.value || 'clash-verge' });

  function resetSecretVisibility() {
    if (!controllerSecret || controllerSecret.type === 'password') return;
    if (togglePasswordInput && controllerSecretToggle) {
      togglePasswordInput(controllerSecret, controllerSecretToggle);
    } else {
      controllerSecret.type = 'password';
    }
  }

  function renderControllerMeta() {
    const isFlClash = syncedClient === 'flclash';
    const automatic = !lastSavedUrl;
    if (controllerMode) {
      controllerMode.textContent = automatic ? '自动读取' : '手动地址';
      controllerMode.className = automatic ? 'glass-pill pill-subtle' : 'glass-pill pill-soft';
    }
    if (controllerUrl) {
      controllerUrl.placeholder = isFlClash
        ? '留空自动读取（默认 9090 端口，如 http://127.0.0.1:9090）'
        : '留空自动读取（如 http://127.0.0.1:9097）';
      controllerUrl.disabled = switchingClient;
    }
    if (controllerSecret) {
      controllerSecret.placeholder = secretSet
        ? '已保存手动密钥，留空保持不变'
        : '留空保持已有 / 自动读取';
      controllerSecret.disabled = switchingClient;
    }
    if (controllerSecretToggle) {
      controllerSecretToggle.disabled = switchingClient;
    }
    if (controllerSecretState) {
      controllerSecretState.textContent = secretSet ? '已设置手动密钥' : '未设置手动密钥';
      controllerSecretState.className = secretSet ? 'glass-pill pill-soft' : 'glass-pill pill-subtle';
    }
    if (controllerClearSecret) {
      controllerClearSecret.disabled = !secretSet || controllerSaving || switchingClient;
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

  function applyControllerState(data, clientChangedOnServer) {
    const ctrl = data.controller || {};
    lastSavedUrl = typeof ctrl.url === 'string' ? ctrl.url : '';
    secretSet = Boolean(ctrl.secret_set);

    if (clientChangedOnServer && !controllerSaving) {
      urlDirty = false;
      secretDirty = false;
      draftClient = syncedClient;
      if (controllerUrl) {
        controllerUrl.value = lastSavedUrl;
        controllerUrl.removeAttribute('aria-invalid');
      }
      if (controllerSecret) {
        controllerSecret.value = '';
        controllerSecret.removeAttribute('aria-invalid');
        resetSecretVisibility();
      }
    } else {
      if (controllerUrl && !urlDirty && !controllerSaving && document.activeElement !== controllerUrl) {
        controllerUrl.value = lastSavedUrl;
        controllerUrl.removeAttribute('aria-invalid');
      }
      if (controllerSecret && !secretDirty && !controllerSaving && document.activeElement !== controllerSecret) {
        controllerSecret.value = '';
        controllerSecret.removeAttribute('aria-invalid');
      }
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
          const data = await apiRequest('/api/proxy');
          if (epoch !== refreshEpoch || switchingClient) continue;
          const nextClient = data.client || 'clash-verge';
          const clientChangedOnServer = nextClient !== syncedClient;
          syncedClient = nextClient;
          if (!urlDirty && !secretDirty) draftClient = nextClient;

          client.value = nextClient;
          enabled.checked = data.enabled;
          status.textContent = data.message || '未启用自动切换';
          egress.textContent = data.egress
            ? `当前出口：${data.egress}`
            : '出口随客户端当前选择检测，守护代理组用于程序分流。';
          restore.disabled = !data.restore_available;
          if (data.group && ![...group.options].some(option => option.value === data.group)) {
            group.add(new Option(data.group, data.group));
          }
          group.value = data.group;
          applyControllerState(data, clientChangedOnServer);
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
      const latestUrl = controllerUrl.value.trim();
      const submittedUrl = latestUrl !== lastSavedUrl ? latestUrl : nextUrl;
      if (submittedUrl === lastSavedUrl) {
        urlDirty = false;
        controllerUrl.value = lastSavedUrl;
        controllerUrl.removeAttribute('aria-invalid');
        return true;
      }
      controllerSaving = true;
      renderControllerMeta();
      refreshEpoch += 1;
      try {
        await post('/api/proxy/controller', { client: targetClient, url: submittedUrl });
        if (targetClient === syncedClient) {
          lastSavedUrl = submittedUrl;
          urlDirty = controllerUrl.value.trim() !== submittedUrl;
          controllerUrl.removeAttribute('aria-invalid');
        }
        showToast(submittedUrl ? '外部控制器地址已保存' : '已切换为自动读取本机控制器', 'success');
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
        await refresh();
      }
    });
  }

  function saveSecretDraft() {
    if (!controllerSecret || switchingClient) return Promise.resolve(true);
    const targetClient = draftClient;
    const nextSecret = controllerSecret.value;
    if (targetClient !== syncedClient) return Promise.resolve(true);
    if (!nextSecret) {
      secretDirty = false;
      controllerSecret.removeAttribute('aria-invalid');
      return Promise.resolve(true);
    }
    return runControllerMutation(async () => {
      if (targetClient !== syncedClient || switchingClient) return true;
      const latestSecret = controllerSecret.value || nextSecret;
      if (!latestSecret) {
        secretDirty = false;
        controllerSecret.removeAttribute('aria-invalid');
        return true;
      }
      controllerSaving = true;
      renderControllerMeta();
      refreshEpoch += 1;
      try {
        await post('/api/proxy/controller', {
          client: targetClient,
          url: lastSavedUrl,
          secret: latestSecret
        });
        if (targetClient === syncedClient) {
          secretSet = true;
          if (controllerSecret.value === latestSecret) {
            secretDirty = false;
            controllerSecret.value = '';
            controllerSecret.removeAttribute('aria-invalid');
            resetSecretVisibility();
          } else {
            secretDirty = controllerSecret.value.length > 0;
          }
        }
        showToast('外部控制器访问密钥已保存', 'success');
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
        await refresh();
      }
    });
  }

  function clearManualSecret() {
    if (!controllerClearSecret || switchingClient) return Promise.resolve(true);
    const targetClient = syncedClient;
    return runControllerMutation(async () => {
      if (targetClient !== syncedClient || switchingClient) return true;
      controllerSaving = true;
      renderControllerMeta();
      refreshEpoch += 1;
      const clearingDraft = controllerSecret?.value || '';
      try {
        await post('/api/proxy/controller', {
          client: targetClient,
          url: lastSavedUrl,
          clear_secret: true
        });
        if (targetClient === syncedClient) {
          secretSet = false;
          if (controllerSecret && controllerSecret.value === clearingDraft) {
            secretDirty = false;
            controllerSecret.value = '';
            controllerSecret.removeAttribute('aria-invalid');
            resetSecretVisibility();
          } else {
            secretDirty = Boolean(controllerSecret?.value);
          }
        }
        showToast('已清除手动密钥', 'success');
        return true;
      } catch (error) {
        showToast(error.message, 'error');
        return false;
      } finally {
        controllerSaving = false;
        renderControllerMeta();
        await refresh();
      }
    });
  }

  if (controllerUrl) {
    controllerUrl.addEventListener('focus', () => {
      if (!urlDirty) draftClient = syncedClient;
    });
    controllerUrl.addEventListener('input', () => {
      draftClient = syncedClient;
      urlDirty = controllerUrl.value.trim() !== lastSavedUrl;
      controllerUrl.removeAttribute('aria-invalid');
    });
    controllerUrl.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        controllerUrl.blur();
      }
    });
    controllerUrl.addEventListener('blur', () => {
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
      secretDirty = controllerSecret.value.length > 0;
      controllerSecret.removeAttribute('aria-invalid');
    });
    controllerSecret.addEventListener('keydown', event => {
      if (event.key === 'Enter') {
        event.preventDefault();
        controllerSecret.blur();
      }
    });
    controllerSecret.addEventListener('blur', event => {
      if (event.relatedTarget === controllerSecretToggle) return;
      if (secretDirty || controllerSecret.value.length > 0) {
        if (!secretDirty) draftClient = syncedClient;
        saveSecretDraft();
      }
    });
  }

  if (controllerSecretToggle) {
    controllerSecretToggle.addEventListener('mousedown', event => {
      if (document.activeElement === controllerSecret) event.preventDefault();
    });
    controllerSecretToggle.addEventListener('click', () => {
      if (togglePasswordInput) {
        togglePasswordInput(controllerSecret, controllerSecretToggle);
      } else if (controllerSecret) {
        controllerSecret.type = controllerSecret.type === 'password' ? 'text' : 'password';
      }
    });
    controllerSecretToggle.addEventListener('blur', event => {
      if (event.relatedTarget === controllerSecret) return;
      if (secretDirty) saveSecretDraft();
    });
  }

  if (controllerClearSecret) {
    controllerClearSecret.addEventListener('click', clearManualSecret);
  }

  if (controllerDetails) {
    controllerDetails.addEventListener('toggle', () => {
      if (controllerDetails.open) {
        const card = controllerDetails.closest('.proxy-card');
        if (card && card.scrollHeight > card.clientHeight) {
          const reduced =
            typeof window.matchMedia === 'function' &&
            window.matchMedia('(prefers-reduced-motion: reduce)').matches;
          const targetTop =
            card.scrollTop +
            controllerDetails.getBoundingClientRect().top -
            card.getBoundingClientRect().top -
            10;
          card.scrollTo({ top: Math.max(0, targetTop), behavior: reduced ? 'auto' : 'smooth' });
        }
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

  client.addEventListener('change', async () => {
    const nextClient = client.value;
    client.disabled = true;
    await waitPendingControllerSave();
    switchingClient = true;
    renderControllerMeta();
    try {
      urlDirty = false;
      secretDirty = false;
      lastSavedUrl = '';
      secretSet = false;
      syncedClient = nextClient;
      draftClient = nextClient;
      if (controllerUrl) {
        controllerUrl.value = '';
        controllerUrl.removeAttribute('aria-invalid');
      }
      if (controllerSecret) {
        controllerSecret.value = '';
        controllerSecret.removeAttribute('aria-invalid');
        resetSecretVisibility();
      }
      refreshEpoch += 1;
      await post('/api/proxy/client', { client: nextClient });
      group.replaceChildren(new Option('未选择', ''));
      await onClientChanged?.();
      showToast('已切换代理客户端', 'success');
    } catch (error) {
      syncedClient = '';
      showToast(error.message, 'error');
    } finally {
      switchingClient = false;
      client.disabled = false;
      renderControllerMeta();
      await refresh();
    }
  });

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
