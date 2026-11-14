export function initUpdates({apiRequest, showToast}) {
  const trigger = document.getElementById('btn-update');
  const dialog = document.getElementById('modal-update');
  const action = document.getElementById('btn-update-action');
  const message = document.getElementById('update-message');
  const progress = document.getElementById('update-progress');
  if (trigger.dataset.ready) return;
  trigger.dataset.ready = 'true';
  let state = 'idle', polling = null, pending = false;
  const labels = {idle: '检查更新', error: '重新检查', current: '再次检查', available: '下载更新', ready: '安装并重启', checking: '检查中…', downloading: '下载中…', installing: '安装中…'};
  function stop() { clearTimeout(polling); polling = null; }
  function render(data) {
    state = data.state;
    message.textContent = data.message;
    action.textContent = labels[state] || '检查更新';
    action.disabled = pending || ['checking', 'downloading', 'installing'].includes(state);
    progress.hidden = !['downloading', 'ready'].includes(state);
    progress.value = data.progress || 0;
    if (state === 'available' && !data.can_install) {
      message.textContent += '\n请使用 Windows 桌面发布版安装更新';
      action.disabled = true;
    }
  }
  async function refresh() {
    stop();
    try {
      const data = await apiRequest('/api/update');
      render(data);
      if (dialog.open && ['checking', 'downloading', 'installing'].includes(state)) polling = setTimeout(refresh, 800);
    } catch {
      if (state === 'installing') {
        message.textContent = '正在重启软件…';
        polling = setTimeout(() => location.reload(), 4000);
      } else {
        state = 'error'; message.textContent = '更新服务暂不可用，请重试';
        action.textContent = labels.error; action.disabled = false;
      }
    }
  }
  async function run() {
    if (pending) return;
    const operation = state === 'available' ? 'download' : state === 'ready' ? 'install' : 'check';
    pending = true; action.disabled = true;
    try {
      await apiRequest('/api/update/' + operation, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
      if (operation === 'install') showToast('正在安装更新，稍后自动重启', 'info', 5000);
    } catch (error) { showToast(error.message || '更新未完成，请重试', 'error'); }
    finally { pending = false; await refresh(); }
  }
  trigger.addEventListener('click', async () => {
    if (document.querySelector('dialog[open]')) return;
    dialog.showModal();
    await refresh();
    if (['idle', 'current', 'error'].includes(state)) await run();
  });
  action.addEventListener('click', run);
  for (const id of ['btn-close-update', 'btn-update-cancel']) document.getElementById(id).addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', stop);
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
}
