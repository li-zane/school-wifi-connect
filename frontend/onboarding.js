const STORAGE_KEY = 'njust_onboarding_v1';
const STEPS = [
  {tab: 'panel-config', target: '.account-slots-section .form-slots-grid', title: '填写校园账号', text: '校园网填学号和密码；只用备用 WiFi 可跳过。'},
  {tab: 'panel-config', target: '.fallback-networks-section .section-title-row', title: '添加备用 WiFi', text: '填好网络和密码，靠前的优先连接。'},
  {tab: 'panel-config', target: '#btn-save-config', title: '保存设置', text: '填好后点保存，之后随时能修改。'},
  {tab: 'panel-status', target: '#btn-toggle-service', title: '开始监控', text: '自动检测网络，断网时尝试恢复。'},
  {tab: 'panel-probes', target: '#btn-open-add-probe', title: '认识程序守护', text: '可选添加常用程序，退出后尝试拉起。'}
];

export function initOnboarding({switchTab, isConfigured, getRunning}) {
  const trigger = document.getElementById('btn-onboarding');
  if (!trigger || document.getElementById('onboarding')) return;
  trigger.disabled = false;
  const root = document.createElement('div');
  root.id = 'onboarding';
  root.hidden = true;
  root.innerHTML = `<div class="tour-spotlight" aria-hidden="true"></div>
    <section class="tour-card" role="dialog" aria-modal="false" aria-labelledby="tour-title" aria-describedby="tour-description">
      <div class="tour-top"><span class="tour-kicker">新手引导 <b id="tour-count"></b></span><button type="button" class="tour-skip" aria-label="跳过新手引导">跳过</button></div>
      <h2 id="tour-title"></h2><p id="tour-description"></p>
      <div class="tour-footer"><button type="button" class="tour-prev">上一步</button><div class="tour-dots" aria-hidden="true">${'<i></i>'.repeat(5)}</div><button type="button" class="tour-next">下一步</button></div>
    </section>`;
  document.body.appendChild(root);
  const card = root.querySelector('.tour-card');
  const ring = root.querySelector('.tour-spotlight');
  const next = root.querySelector('.tour-next');
  const previous = root.querySelector('.tour-prev');
  let index = 0, active = false, target = null, observer = null, frame = 0;
  let returnFocus = null, returnTab = 'panel-status', savedScroll = [];
  let storageAvailable = true, seen = false;
  try { seen = Boolean(localStorage.getItem(STORAGE_KEY)); } catch { storageAvailable = false; }

  function remember(state) {
    seen = true;
    try { localStorage.setItem(STORAGE_KEY, state); } catch { /* Keep manual help usable. */ }
  }

  function position() {
    frame = 0;
    if (!active || !target) return;
    const rect = target.getBoundingClientRect();
    if (!rect.width || !rect.height) { finish('skipped', false); return; }
    const pane = target.closest('.tab-panel > .glass-panel');
    const clip = pane?.getBoundingClientRect();
    const left = Math.max(8, rect.left, clip ? clip.left + 4 : 0);
    const right = Math.min(innerWidth - 8, rect.right, clip ? clip.right - 4 : innerWidth);
    const top = Math.max(8, rect.top, clip ? clip.top + 4 : 0);
    const bottom = Math.min(innerHeight - 8, rect.bottom, clip ? clip.bottom - 4 : innerHeight);
    if (right <= left || bottom <= top) { finish('skipped', false); return; }
    ring.hidden = false;
    Object.assign(ring.style, {left: `${left - 4}px`, top: `${top - 4}px`, width: `${right - left + 8}px`, height: `${bottom - top + 8}px`});
    const navigation = document.querySelector('.glass-segmented-tabs');
    const vertical = navigation.getAttribute('aria-orientation') === 'vertical';
    const limit = vertical ? innerHeight - 10 : Math.min(innerHeight - 10, navigation.getBoundingClientRect().top - 10);
    const above = top - 22, below = limit - bottom - 12;
    card.style.maxHeight = `${Math.max(88, Math.max(above, below))}px`;
    const bounds = card.getBoundingClientRect();
    const y = (below >= bounds.height || below >= above)
      ? Math.max(8, Math.min(limit - bounds.height, bottom + 12))
      : Math.max(8, top - bounds.height - 12);
    const x = Math.max(12, Math.min(innerWidth - bounds.width - 12, (left + right - bounds.width) / 2));
    Object.assign(card.style, {left: `${x}px`, top: `${y}px`});
  }

  function requestPosition() {
    if (!frame && active) frame = requestAnimationFrame(position);
  }

  function showStep() {
    const step = STEPS[index];
    switchTab(step.tab);
    target = document.querySelector(step.target);
    if (!target) { finish('skipped'); return; }
    const pane = target.closest('.tab-panel > .glass-panel');
    if (pane) {
      const rect = target.getBoundingClientRect(), area = pane.getBoundingClientRect();
      pane.scrollTop += rect.top - area.top - (pane.clientHeight - rect.height) / 2;
    }
    const running = index === 3 && getRunning();
    card.querySelector('#tour-count').textContent = `${index + 1} / ${STEPS.length}`;
    card.querySelector('#tour-title').textContent = running ? '监控已开启' : step.title;
    card.querySelector('#tour-description').textContent = running ? '这里可以暂停或继续网络守护。' : step.text;
    previous.disabled = index === 0;
    next.textContent = index === STEPS.length - 1 ? '完成' : '下一步';
    root.querySelectorAll('.tour-dots i').forEach((dot, n) => dot.classList.toggle('selected', n === index));
    observer?.disconnect();
    observer = new ResizeObserver(requestPosition);
    observer.observe(target);
    observer.observe(target.closest('.account-slots-section, .fallback-networks-section') || target);
    observer.observe(card);
    position();
    if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
      card.getAnimations().forEach(animation => animation.cancel());
      card.animate([{opacity: .4, transform: 'translateY(6px)'}, {opacity: 1, transform: 'translateY(0)'}], {duration: 180, easing: 'ease-out'});
    }
    next.focus({preventScroll: true});
  }

  function onKey(event) {
    if (event.key === 'Escape' && !document.querySelector('dialog[open]')) {
      event.preventDefault(); finish('skipped');
    }
  }

  function finish(state, restore = true) {
    if (!active) return;
    active = false;
    remember(state);
    observer?.disconnect(); observer = null;
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    window.removeEventListener('resize', requestPosition);
    window.removeEventListener('scroll', requestPosition, true);
    document.removeEventListener('keydown', onKey);
    card.getAnimations().forEach(animation => animation.cancel());
    root.hidden = true;
    target = null;
    if (restore) {
      switchTab(state === 'done' ? 'panel-status' : returnTab);
      savedScroll.forEach(([element, offset]) => { element.scrollTop = offset; });
    }
    const focus = returnFocus?.isConnected && returnFocus !== document.body && !returnFocus.closest('[hidden]') ? returnFocus : trigger;
    focus.focus({preventScroll: true});
  }

  function start() {
    if (document.querySelector('dialog[open]')) return;
    if (!active) {
      returnFocus = document.activeElement;
      returnTab = document.querySelector('.tab-panel.active')?.id || 'panel-status';
      savedScroll = [...document.querySelectorAll('.tab-panel > .glass-panel')].map(element => [element, element.scrollTop]);
      active = true;
      root.hidden = false;
      window.addEventListener('resize', requestPosition);
      window.addEventListener('scroll', requestPosition, true);
      document.addEventListener('keydown', onKey);
    }
    index = 0;
    showStep();
  }

  trigger.addEventListener('click', start);
  root.querySelector('.tour-skip').addEventListener('click', () => finish('skipped'));
  previous.addEventListener('click', () => { if (index > 0) { index--; showStep(); } });
  next.addEventListener('click', () => { if (index < STEPS.length - 1) { index++; showStep(); } else finish('done'); });
  if (!seen && storageAvailable && !isConfigured) start();
}
