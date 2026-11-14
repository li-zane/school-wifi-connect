export function initProxy({apiRequest, showToast}) {
  const enabled = document.getElementById('proxy-fail-open');
  const group = document.getElementById('proxy-group');
  const status = document.getElementById('proxy-status');
  const restore = document.getElementById('proxy-restore');
  const detect = document.getElementById('proxy-detect');
  let refreshing = false;
  const post = (path, payload) => apiRequest(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const data = await apiRequest('/api/proxy');
      enabled.checked = data.enabled;
      status.textContent = data.enabled ? data.message : '未启用自动切换';
      restore.disabled = !data.restore_available;
      if (data.group && ![...group.options].some(option => option.value === data.group)) group.add(new Option(data.group, data.group));
      group.value = data.group;
    } catch { status.textContent = '服务待连接'; }
    finally { refreshing = false; }
  }
  enabled.addEventListener('change', async () => {
    const selected = enabled.checked; enabled.disabled = true;
    try {
      await post('/api/config', {proxy_fail_open:selected});
      showToast(selected ? '已启用代理故障自动切换' : '已关闭自动切换', 'success');
    } catch(error) { showToast(error.message,'error'); }
    finally { enabled.disabled = false; refresh(); }
  });
  detect.addEventListener('click', async () => {
    detect.disabled = true;
    try {
      const selected = group.value;
      const data = await apiRequest('/api/proxy/groups');
      group.replaceChildren(new Option('请选择代理组',''));
      for (const name of data.groups) group.add(new Option(name,name));
      group.value = selected;
      showToast('代理客户端已连接', 'success');
    } catch(error) { showToast(error.message,'error'); }
    finally { detect.disabled = false; }
  });
  group.addEventListener('change', async () => {
    group.disabled = true;
    try { await post('/api/proxy/group', {group:group.value}); showToast('代理组已设置', 'success'); }
    catch(error) { showToast(error.message,'error'); }
    finally { group.disabled = false; refresh(); }
  });
  restore.addEventListener('click', async () => {
    restore.disabled = true;
    try { await post('/api/proxy/restore', {}); showToast('已恢复代理设置', 'success'); }
    catch(error) { showToast(error.message,'error'); }
    finally { refresh(); }
  });
  refresh();
  return {refresh};
}
