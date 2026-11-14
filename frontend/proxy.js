export function initProxy({apiRequest, showToast, onClientChanged}) {
  const enabled = document.getElementById('proxy-fail-open');
  const group = document.getElementById('proxy-group');
  const status = document.getElementById('proxy-status');
  const restore = document.getElementById('proxy-restore');
  const detect = document.getElementById('proxy-detect');
  const check = document.getElementById('proxy-check');
  const egress = document.getElementById('proxy-egress');
  const client = document.getElementById('proxy-client');
  let refreshing = false;
  const post = (path, payload) => apiRequest(path, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const data = await apiRequest('/api/proxy');
      client.value = data.client || 'clash-verge';
      enabled.checked = data.enabled;
      status.textContent = data.message || '未启用自动切换';
      egress.textContent = data.egress ? `当前出口：${data.egress}` : '出口随客户端当前选择检测，守护代理组用于程序分流。';
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
  client.addEventListener('change', async () => {
    client.disabled = true;
    try {
      await post('/api/proxy/client',{client:client.value});
      group.replaceChildren(new Option('未选择',''));
      await onClientChanged?.();
      showToast('已切换代理客户端','success');
    } catch(error) { showToast(error.message,'error'); }
    finally { client.disabled=false; refresh(); }
  });
  check.addEventListener('click', async () => {
    check.disabled = true;
    try {
      await post('/api/proxy/check', {});
      showToast(enabled.checked ? '正在检测当前代理出口' : '正在检测出口；自动关闭尚未开启', 'info');
    } catch(error) { showToast(error.message, 'error'); }
    finally { check.disabled = false; refresh(); }
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
