const GUIDE_DATA = {
  'clash-verge': {
    label: 'Clash Verge',
    steps: [
      {
        title: '进入「外部控制」设置',
        path: ['设置', 'Clash 设置', '外部控制'],
        text: '打开 Clash Verge，在左侧栏进入「设置」，于「Clash 设置」区域点击「外部控制」。',
        image: 'guide/verge-settings.webp',
        alt: 'Clash Verge 设置页面截图，右侧 Clash 设置区域中包含外部控制入口'
      },
      {
        title: '开启外部控制并保存',
        path: ['启用外部控制器', '监听地址', 'API 访问密钥', '保存'],
        text: '打开「启用外部控制器」，监听地址设为本机 127.0.0.1（如图中 127.0.0.1:9097），按需设置「API 访问密钥」后点击「保存」。',
        image: 'guide/verge-controller.webp',
        alt: 'Clash Verge 外部控制器监听地址弹窗截图，包含启用开关、监听地址、已遮盖的 API 访问密钥与保存按钮'
      }
    ]
  },
  flclash: {
    label: 'FlClash',
    steps: [
      {
        title: '进入「常规」设置',
        path: ['工具', '设置', '常规'],
        text: '打开 FlClash，点击左侧「工具」图标，在「设置」列表底部选择「常规」。',
        image: 'guide/flclash-settings.webp',
        alt: 'FlClash 工具菜单页面截图，下方设置列表中包含常规入口'
      },
      {
        title: '开启「外部控制器」',
        path: ['常规', '入站', '外部控制器', '默认 9090'],
        text: '进入「入站」区域打开「外部控制器」开关（默认 9090 端口），开启后按需查看或设置访问密钥；本软件仅连接本机 127.0.0.1。',
        image: 'guide/flclash-controller.webp',
        alt: 'FlClash 常规设置的入站区域截图，显示外部控制器开关（当前关闭）与默认 9090 端口提示'
      }
    ]
  }
};

const prefersReducedMotion = () =>
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export function initProxyGuide({ getClient } = {}) {
  const trigger = document.getElementById('proxy-guide-open');
  const modal = document.getElementById('modal-proxy-guide');
  const zoomModal = document.getElementById('modal-proxy-guide-zoom');
  if (!trigger || !modal || !zoomModal || trigger.dataset.ready) return;
  trigger.dataset.ready = 'true';

  const tabButtons = [...modal.querySelectorAll('[data-guide-client]')];
  const stepStage = modal.querySelector('.guide-step-stage');
  const stepBadge = document.getElementById('proxy-guide-step-badge');
  const stepPath = document.getElementById('proxy-guide-step-path');
  const stepTitle = document.getElementById('proxy-guide-step-title');
  const stepText = document.getElementById('proxy-guide-step-text');
  const shotBtn = document.getElementById('proxy-guide-shot-btn');
  const shotImg = document.getElementById('proxy-guide-shot-img');
  const dotsWrap = document.getElementById('proxy-guide-dots');
  const prevBtn = document.getElementById('proxy-guide-prev');
  const nextBtn = document.getElementById('proxy-guide-next');
  const closeBtn = document.getElementById('proxy-guide-close');

  const zoomClient = document.getElementById('proxy-guide-zoom-client');
  const zoomTitle = document.getElementById('proxy-guide-zoom-title');
  const zoomToggle = document.getElementById('proxy-guide-zoom-toggle');
  const zoomClose = document.getElementById('proxy-guide-zoom-close');
  const zoomStage = document.getElementById('proxy-guide-zoom-stage');
  const zoomImg = document.getElementById('proxy-guide-zoom-img');

  let activeClient = 'clash-verge';
  let stepIndex = 0;
  let returnFocus = null;
  let zoomReturnFocus = null;
  let modalBackdropDown = false;
  let zoomBackdropDown = false;
  let zoomedIn = false;

  function currentPack() {
    return GUIDE_DATA[activeClient] || GUIDE_DATA['clash-verge'];
  }

  function currentStep() {
    const pack = currentPack();
    const index = Math.max(0, Math.min(stepIndex, pack.steps.length - 1));
    return { pack, step: pack.steps[index], index, total: pack.steps.length };
  }

  function animateOnce(el, keyframes, options) {
    if (!el || prefersReducedMotion() || typeof el.animate !== 'function') return;
    el.getAnimations().forEach(anim => anim.cancel());
    el.animate(keyframes, options);
  }

  function renderPath(parts) {
    stepPath.replaceChildren();
    parts.forEach((part, idx) => {
      if (idx > 0) {
        const sep = document.createElement('span');
        sep.className = 'guide-path-sep';
        sep.setAttribute('aria-hidden', 'true');
        sep.textContent = '›';
        stepPath.appendChild(sep);
      }
      const chip = document.createElement('span');
      chip.className = 'guide-path-chip';
      chip.textContent = part;
      stepPath.appendChild(chip);
    });
  }

  function renderDots(index, total) {
    dotsWrap.replaceChildren();
    for (let i = 0; i < total; i += 1) {
      const dot = document.createElement('i');
      dot.className = i === index ? 'guide-dot is-active' : 'guide-dot';
      dotsWrap.appendChild(dot);
    }
  }

  function render(animate = false) {
    const { pack, step, index, total } = currentStep();
    stepIndex = index;

    tabButtons.forEach(btn => {
      const selected = btn.dataset.guideClient === activeClient;
      btn.setAttribute('aria-selected', String(selected));
      btn.tabIndex = selected ? 0 : -1;
    });

    stepBadge.textContent = `步骤 ${index + 1} / ${total}`;
    stepTitle.textContent = step.title;
    stepText.textContent = step.text;
    renderPath(step.path);

    if (shotImg.getAttribute('src') !== step.image) {
      shotImg.src = step.image;
    }
    shotImg.alt = step.alt;
    shotBtn.setAttribute('aria-label', `放大查看${pack.label}截图：${step.title}`);

    renderDots(index, total);
    prevBtn.disabled = index === 0;
    nextBtn.textContent = index === total - 1 ? '完成' : '下一页';

    if (animate) {
      animateOnce(
        stepStage,
        [{ opacity: 0.55, transform: 'translateY(4px)' }, { opacity: 1, transform: 'translateY(0)' }],
        { duration: 160, easing: 'ease-out' }
      );
    }
  }

  function setZoomScale(nextZoom) {
    zoomedIn = Boolean(nextZoom);
    zoomStage.classList.toggle('is-zoomed', zoomedIn);
    zoomToggle.textContent = zoomedIn ? '适应窗口' : '1.5× 细节';
    zoomToggle.setAttribute('aria-pressed', String(zoomedIn));
  }

  function openGuide() {
    if (modal.open || document.querySelector('dialog[open]')) return;
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : trigger;
    const client = getClient?.();
    activeClient = client === 'flclash' ? 'flclash' : 'clash-verge';
    stepIndex = 0;
    render(false);
    modal.showModal();
    animateOnce(
      modal.querySelector('.proxy-guide-card'),
      [{ opacity: 0.72, transform: 'translateY(6px) scale(0.99)' }, { opacity: 1, transform: 'translateY(0) scale(1)' }],
      { duration: 180, easing: 'ease-out' }
    );
    const activeTab = tabButtons.find(btn => btn.dataset.guideClient === activeClient);
    (activeTab || nextBtn).focus({ preventScroll: true });
  }

  function openZoom() {
    if (!modal.open || zoomModal.open) return;
    const { pack, step } = currentStep();
    zoomReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : shotBtn;
    zoomClient.textContent = pack.label;
    zoomTitle.textContent = step.title;
    zoomImg.src = step.image;
    zoomImg.alt = step.alt;
    setZoomScale(false);
    zoomModal.showModal();
    animateOnce(
      zoomModal.querySelector('.proxy-guide-zoom-card'),
      [{ opacity: 0.75, transform: 'scale(0.97)' }, { opacity: 1, transform: 'scale(1)' }],
      { duration: 160, easing: 'ease-out' }
    );
    zoomClose.focus({ preventScroll: true });
  }

  function restoreFocus(target, fallback) {
    const el = target?.isConnected && target !== document.body && !target.closest('[hidden]') ? target : fallback;
    el?.focus({ preventScroll: true });
  }

  trigger.addEventListener('click', openGuide);
  closeBtn.addEventListener('click', () => modal.close());

  tabButtons.forEach((btn, idx) => {
    btn.addEventListener('click', () => {
      if (activeClient === btn.dataset.guideClient) return;
      activeClient = btn.dataset.guideClient;
      stepIndex = 0;
      render(true);
    });

    btn.addEventListener('keydown', event => {
      let nextIdx = -1;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        nextIdx = (idx + 1) % tabButtons.length;
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        nextIdx = (idx + tabButtons.length - 1) % tabButtons.length;
      } else if (event.key === 'Home') {
        nextIdx = 0;
      } else if (event.key === 'End') {
        nextIdx = tabButtons.length - 1;
      }
      if (nextIdx < 0) return;
      event.preventDefault();
      const targetTab = tabButtons[nextIdx];
      activeClient = targetTab.dataset.guideClient;
      stepIndex = 0;
      render(true);
      targetTab.focus({ preventScroll: true });
    });
  });

  prevBtn.addEventListener('click', () => {
    if (stepIndex > 0) {
      stepIndex -= 1;
      render(true);
    }
  });

  nextBtn.addEventListener('click', () => {
    const { total } = currentStep();
    if (stepIndex < total - 1) {
      stepIndex += 1;
      render(true);
    } else {
      modal.close();
    }
  });

  shotBtn.addEventListener('click', openZoom);
  zoomToggle.addEventListener('click', () => setZoomScale(!zoomedIn));
  zoomImg.addEventListener('click', () => setZoomScale(!zoomedIn));
  zoomClose.addEventListener('click', () => zoomModal.close());

  modal.addEventListener('pointerdown', event => {
    modalBackdropDown = event.target === modal;
  });
  modal.addEventListener('click', event => {
    if (modalBackdropDown && event.target === modal) modal.close();
    modalBackdropDown = false;
  });

  zoomModal.addEventListener('pointerdown', event => {
    zoomBackdropDown = event.target === zoomModal;
  });
  zoomModal.addEventListener('click', event => {
    if (zoomBackdropDown && event.target === zoomModal) zoomModal.close();
    zoomBackdropDown = false;
  });

  zoomModal.addEventListener('close', () => {
    setZoomScale(false);
    restoreFocus(zoomReturnFocus, shotBtn);
    zoomReturnFocus = null;
  });

  modal.addEventListener('close', () => {
    if (zoomModal.open) zoomModal.close();
    restoreFocus(returnFocus, trigger);
    returnFocus = null;
  });
}
