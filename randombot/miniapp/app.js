const tg = window.Telegram?.WebApp;

let preferences = {
  themeAuto: true,
  haptics: true,
  animations: true,
};

function setMetric(id, value) {
  const node = document.getElementById(id);
  if (node) node.textContent = value;
}

function notify(action, extra = {}) {
  if (tg) {
    tg.sendData(JSON.stringify({ action, ...extra }));
  }
}

function triggerHaptic(type = 'light') {
  if (!preferences.haptics) return;
  if (tg?.HapticFeedback?.impactOccurred) {
    tg.HapticFeedback.impactOccurred(type);
  } else if (navigator.vibrate) {
    navigator.vibrate(12);
  }
}

function showToast(text) {
  const toast = document.getElementById('toast');
  toast.textContent = text;
  toast.classList.add('show');
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => toast.classList.remove('show'), 2200);
}

function setLoading(on, text = 'Подготавливаем оплату…') {
  const loading = document.getElementById('loadingScreen');
  const label = loading.querySelector('.loading-text');
  label.textContent = text;
  loading.classList.toggle('show', on);
}

function switchView(viewName) {
  document.querySelectorAll('.view').forEach((view) => view.classList.toggle('active', view.classList.contains(`${viewName}-view`)));
}

function applyTheme(theme) {
  const resolved = theme === 'dark' ? 'dark' : 'light';
  document.body.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
  const themePill = document.getElementById('themePill');
  const themeStatusText = document.getElementById('themeStatusText');
  if (themePill) {
    themePill.textContent = resolved === 'dark' ? 'Dark' : 'Light';
  }
  if (themeStatusText) {
    themeStatusText.textContent = preferences.themeAuto ? 'Синхронизация с Telegram' : `Режим: ${resolved === 'dark' ? 'тёмный' : 'светлый'}`;
  }
  if (tg) {
    tg.setHeaderColor(resolved === 'dark' ? '#07111f' : '#f4f7ff');
    tg.setBackgroundColor(resolved === 'dark' ? '#07111f' : '#f4f7ff');
  }
}

function loadPreferences() {
  const stored = localStorage.getItem('creator-miniapp-settings');
  if (stored) {
    preferences = { ...preferences, ...JSON.parse(stored) };
  }
  document.body.classList.toggle('reduced-motion', !preferences.animations);
}

function savePreferences() {
  localStorage.setItem('creator-miniapp-settings', JSON.stringify(preferences));
}

function getOrientation() {
  if (screen.orientation && screen.orientation.type) {
    return screen.orientation.type.startsWith('landscape') ? 'landscape' : 'portrait';
  }
  return window.innerWidth > window.innerHeight ? 'landscape' : 'portrait';
}

function refreshLayout() {
  const orientation = getOrientation();
  document.documentElement.dataset.orientation = orientation;
  document.body.dataset.orientation = orientation;

  const vh = window.innerHeight * 0.01;
  document.documentElement.style.setProperty('--vh', `${vh}px`);

  if (tg) {
    tg.expand();
  }

  // Force reflow so layout (grids/flex directions driven by CSS media queries)
  // recalculates immediately instead of waiting for the next paint.
  const appShell = document.querySelector('.app-shell');
  if (appShell) {
    void appShell.offsetHeight;
  }

  // Make sure all interactive elements stay within the visible viewport
  // in the new orientation.
  document.querySelectorAll('.btn, .icon-btn, .pay-card, .premium-action').forEach((el) => {
    el.style.maxWidth = '100%';
  });
}

function handleOrientationChange() {
  // Give the browser a moment to finish resizing before recalculating layout.
  window.setTimeout(refreshLayout, 100);
  window.setTimeout(refreshLayout, 400);
}

window.addEventListener('orientationchange', handleOrientationChange);
window.addEventListener('resize', () => {
  window.clearTimeout(handleOrientationChange.resizeTimeout);
  handleOrientationChange.resizeTimeout = window.setTimeout(refreshLayout, 150);
});
if (screen.orientation && screen.orientation.addEventListener) {
  screen.orientation.addEventListener('change', handleOrientationChange);
}

window.addEventListener('DOMContentLoaded', () => {
  loadPreferences();
  refreshLayout();
  if (tg) {
    tg.ready();
    tg.expand();
    tg.enableClosingConfirmation();
    if (tg.onEvent) {
      tg.onEvent('viewportChanged', refreshLayout);
    }
  }


  const user = tg?.initDataUnsafe?.user || {};
  const avatar = document.getElementById('avatar');
  const profileName = document.getElementById('profileName');
  const profileHandle = document.getElementById('profileHandle');

  if (avatar) {
    avatar.src = user.photo_url || 'https://via.placeholder.com/96';
  }
  if (profileName) {
    profileName.textContent = `${user.first_name || 'Creator'} ${user.last_name || ''}`.trim() || 'Creator';
  }
  if (profileHandle) {
    profileHandle.textContent = user.username ? `@${user.username}` : 'Без username';
  }

  notify('register_profile', {
    user: {
      username: user.username || null,
      first_name: user.first_name || null,
      last_name: user.last_name || null,
      photo_url: user.photo_url || null,
    },
  });

  const themeValue = preferences.themeAuto ? (tg?.colorScheme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) : (document.body.dataset.theme || 'light');
  applyTheme(themeValue);

  setMetric('activeGiveaways', '12');
  setMetric('monthlyIncome', '3,240 ⭐');
  setMetric('securityMode', 'Strict');

  const bindButton = (id, handler) => {
    const btn = document.getElementById(id);
    if (btn) {
      btn.addEventListener('click', () => {
        triggerHaptic('light');
        handler();
      });
    }
  };

  bindButton('createGiveawayBtn', () => {
    notify('create_giveaway');
    showToast('Открыт мастер создания розыгрыша');
  });

  bindButton('boostBtn', () => {
    notify('buy_boost');
    showToast('Переход к Boost');
  });

  bindButton('featuredBtn', () => {
    notify('buy_featured');
    showToast('Переход к Featured');
  });

  bindButton('premiumBtn', () => {
    switchView('premium');
    showToast('Открыта премиум-панель');
  });

  bindButton('settingsBtn', () => {
    switchView('settings');
    showToast('Открыты настройки');
  });

  bindButton('backToDashboardBtn', () => switchView('dashboard'));
  bindButton('backToDashboardFromSettingsBtn', () => switchView('dashboard'));
  bindButton('closeSettingsBtn', () => switchView('dashboard'));

  bindButton('statsBtn', () => {
    notify('request_stats');
    showToast('Статистика отправлена в бот');
  });

  document.querySelectorAll('.pay-card').forEach((card) => {
    card.addEventListener('click', () => {
      triggerHaptic('medium');
      const method = card.dataset.payment;
      setLoading(true, method === 'stars' ? 'Открываем Telegram Stars…' : 'Открываем CryptoBot…');
      window.setTimeout(() => {
        setLoading(false);
        if (method === 'stars') {
          notify('pay_stars', { source: 'dashboard' });
          showToast('Открыт поток Stars');
        } else {
          notify('pay_crypto', { source: 'dashboard' });
          showToast('Открыт поток Crypto');
        }
      }, 450);
    });
  });

  document.querySelectorAll('.premium-action').forEach((button) => {
    button.addEventListener('click', () => {
      triggerHaptic('medium');
      const plan = button.dataset.plan;
      const method = button.dataset.payment;
      setLoading(true, method === 'stars' ? 'Подготовка Stars-платежа…' : 'Подготовка Crypto-платежа…');
      window.setTimeout(() => {
        setLoading(false);
        if (method === 'stars') {
          notify('pay_stars', { plan, source: 'premium' });
          showToast('Платёж Stars запущен');
        } else {
          notify('pay_crypto', { plan, source: 'premium' });
          showToast('Платёж Crypto запущен');
        }
      }, 450);
    });
  });

  const themeAutoToggle = document.getElementById('themeAutoToggle');
  const hapticsToggle = document.getElementById('hapticsToggle');
  const animationsToggle = document.getElementById('animationsToggle');

  if (themeAutoToggle) {
    themeAutoToggle.checked = preferences.themeAuto;
    themeAutoToggle.addEventListener('change', (event) => {
      preferences.themeAuto = event.target.checked;
      savePreferences();
      const nextTheme = preferences.themeAuto ? (tg?.colorScheme || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) : (document.body.dataset.theme === 'dark' ? 'dark' : 'light');
      applyTheme(nextTheme);
      showToast('Тема обновлена');
    });
  }

  if (hapticsToggle) {
    hapticsToggle.checked = preferences.haptics;
    hapticsToggle.addEventListener('change', (event) => {
      preferences.haptics = event.target.checked;
      savePreferences();
      showToast(preferences.haptics ? 'Вибро отклик включён' : 'Вибро отклик выключен');
    });
  }

  if (animationsToggle) {
    animationsToggle.checked = preferences.animations;
    animationsToggle.addEventListener('change', (event) => {
      preferences.animations = event.target.checked;
      document.body.classList.toggle('reduced-motion', !preferences.animations);
      savePreferences();
      showToast(preferences.animations ? 'Анимации включены' : 'Анимации выключены');
    });
  }
});
