/**
 * theme.js —— 明暗主题切换
 *
 * theme.css 里定义了完整的 .dark 令牌覆盖，但原版把
 * <html class="light"> 写死，全站也没有任何切换代码，
 * 那一整套暗色变量属于永远执行不到的死代码。
 *
 * 这里补上切换逻辑：支持 light / dark / auto 三态，
 * auto 跟随系统 prefers-color-scheme 实时变化。
 */

import { getTheme, saveTheme } from './store.js';
import { refreshIcons } from './dom-utils.js';

/** 系统深色偏好的媒体查询对象 */
const darkQuery = window.matchMedia('(prefers-color-scheme: dark)');

/**
 * 把主题真正应用到 <html> 上。
 * @param {string} theme - "light" | "dark" | "auto"
 */
function applyTheme(theme) {
  const isDark = theme === 'dark' || (theme === 'auto' && darkQuery.matches);
  const root = document.documentElement;
  root.classList.toggle('dark', isDark);
  root.classList.toggle('light', !isDark);
  // 让浏览器把滚动条、表单控件也切成对应配色
  root.style.colorScheme = isDark ? 'dark' : 'light';
}

/**
 * 更新主题切换按钮的图标与提示文案。
 * @param {HTMLElement} btn - 切换按钮
 * @param {string} theme - 当前主题
 */
function updateToggleButton(btn, theme) {
  if (!btn) return;
  const iconMap = { light: 'sun', dark: 'moon', auto: 'monitor' };
  const labelMap = { light: '浅色模式', dark: '深色模式', auto: '跟随系统' };
  btn.innerHTML = `<i data-lucide="${iconMap[theme]}"></i>`;
  btn.title = labelMap[theme] + '（点击切换）';
  btn.setAttribute('aria-label', labelMap[theme]);
  refreshIcons();
}

/**
 * 初始化主题系统：应用已保存的偏好并绑定切换按钮。
 * @param {HTMLElement} toggleBtn - 主题切换按钮
 */
export function initTheme(toggleBtn) {
  let current = getTheme();
  applyTheme(current);
  updateToggleButton(toggleBtn, current);

  // auto 模式下跟随系统变化实时切换
  darkQuery.addEventListener('change', () => {
    if (getTheme() === 'auto') applyTheme('auto');
  });

  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      // 三态循环：auto → light → dark → auto
      const order = ['auto', 'light', 'dark'];
      current = order[(order.indexOf(current) + 1) % order.length];
      saveTheme(current);
      applyTheme(current);
      updateToggleButton(toggleBtn, current);
    });
  }
}
