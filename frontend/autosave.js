// One serialized queue; never reload form values after a response.
export function initAutoSave({apiRequest, showToast, ready, fallbacks, onSaved}) {
  const form = document.getElementById('form-config');
  const status = document.getElementById('autosave-state');
  const fields = {'cfg-ssid':'ssid','cfg-interval':'interval_minutes','cfg-domain':'domain',
    'cfg-timeout':'connectivity_timeout_ms','cfg-checks':'connectivity_checks',
    'cfg-remember':'remember_credentials','cfg-startup':'startup_enabled',
    'cfg-auto-monitor':'auto_start_monitor','cfg-show-portal':'show_portal','cfg-close-behavior':'close_behavior'};
  let pending = {}, rejected = {}, working = null, timer;
  const errors = new Map();
  const dirty = new Set();
  const value = id => { const el = document.getElementById(id); return el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value; };
  function indicate(text, error = false) { status.textContent = text; status.dataset.state = error ? 'error' : 'saved'; }
  function enqueue(payload) {
    pending = {...rejected, ...pending, ...payload}; rejected = {}; indicate('待保存…');
    clearTimeout(timer); timer = setTimeout(drain, 150);
  }
  function credentials() {
    return {username: value('cfg-username').trim(), password: value('cfg-password'), remember_credentials:value('cfg-remember')};
  }
  function queueField(el) {
    dirty.delete(el);
    if (el.closest('#fallback-networks-list')) queueFallbacks();
    else if (['cfg-username','cfg-password','cfg-remember'].includes(el.id)) enqueue(credentials());
    else if (fields[el.id]) enqueue({[fields[el.id]]: value(el.id)});
  }
  function queueFallbacks() { enqueue({fallback_networks: fallbacks()}); }
  async function drain() {
    if (working) return working;
    working = (async () => {
      while (Object.keys(pending).length) {
        if (!ready()) { clearTimeout(timer); timer = setTimeout(drain, 250); return; }
        const payload = pending; pending = {};
        indicate('正在保存…');
        try {
          await apiRequest('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
          for (const key of Object.keys(payload)) errors.delete(key);
          onSaved?.(); indicate(errors.size ? '有未保存设置' : dirty.size ? '编辑中' : '已自动保存', Boolean(errors.size));
        } catch (error) {
          rejected = {...rejected, ...payload};
          if (Object.keys(pending).length) { pending = {...rejected, ...pending}; rejected = {}; }
          for (const key of Object.keys(payload)) errors.set(key,error.message);
          indicate(error.message, true); showToast(`保存失败：${error.message}`, 'error');
        }
      }
    })();
    try { await working; } finally { working = null; }
  }
  form.addEventListener('input', event => {
    if (event.target.matches('input:not([type=checkbox]),textarea')) { dirty.add(event.target); indicate('编辑中'); }
  });
  form.addEventListener('focusout', event => {
    if (dirty.has(event.target)) queueField(event.target);
  });
  form.addEventListener('change', event => {
    if (event.target.matches('input[type=checkbox],select')) queueField(event.target);
  });
  async function flush() {
    for (const el of [...dirty]) queueField(el);
    clearTimeout(timer);
    // Mutations are bounded by the HTTP layer; wait for an in-flight action.
    const deadline = Date.now() + 15000;
    while (working || Object.keys(pending).length) {
      if (Date.now() > deadline) { indicate('保存尚未完成，请稍后再试', true); return false; }
      if (working) await working;
      else if (ready()) await drain();
      else await new Promise(resolve => setTimeout(resolve, 100));
    }
    return errors.size === 0;
  }
  return {queueFallbacks, flush, saveAll:flush};
}
