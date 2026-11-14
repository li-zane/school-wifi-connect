export function initWindow({apiRequest, showToast, flush}) {
  if (!window.NJUST_DESKTOP || !window.chrome?.webview) return;
  document.documentElement.classList.add('native-desktop');
  document.querySelector('.window-controls').hidden = false;
  const post = command => window.chrome.webview.postMessage(`window:${command}`);
  post('ready');
  const modal = document.getElementById('modal-close');
  let closing = false;
  for (const [id, command] of [['window-min','minimize'],['window-max','maximize'],['window-close','close']]) {
    document.getElementById(id).addEventListener('click', event => { event.currentTarget.blur(); post(command); });
  }
  document.querySelectorAll('[data-window-drag]').forEach(surface => {
    surface.addEventListener('pointerdown', event => {
      if (event.button === 0 && !event.target.closest('button,input,select,a')) post('drag');
    });
    surface.addEventListener('dblclick', event => { if (!event.target.closest('button')) post('maximize'); });
  });
  window.addEventListener('njust-close-request', async () => {
    if (closing || modal.open) return;
    closing = true;
    try {
      if (!await flush()) return;
      const config = await apiRequest('/api/config');
      if (['tray','exit'].includes(config.close_behavior)) post(config.close_behavior);
      else { document.getElementById('close-remember').checked = false; modal.showModal(); document.getElementById('close-tray').focus(); }
    } catch(error) { showToast(error.message, 'error'); }
    finally { closing = false; }
  });
  document.getElementById('close-cancel').addEventListener('click', () => modal.close());
  for (const action of ['tray','exit']) document.getElementById(`close-${action}`).addEventListener('click', async () => {
    if (closing) return;
    closing = true;
    try {
      if (document.getElementById('close-remember').checked) {
        await apiRequest('/api/config', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({close_behavior:action})});
        document.getElementById('cfg-close-behavior').value = action;
      }
      modal.close(); if (action === 'exit') showToast('正在退出…'); post(action);
    } catch(error) { showToast(error.message, 'error'); }
    finally { closing = false; }
  });
}
