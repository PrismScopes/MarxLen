/**
 * dom-utils.js —— DOM 操作、事件绑定、样式工具
 *
 * 包裹常见的 querySelector、createElement、classList 操作，
 * 减少主逻辑里的重复代码；同时做基础的 XSS 转义。
 */

import { MOBILE_BREAKPOINT, SCROLL_BOTTOM_THRESHOLD } from './config.js';

/**
 * 快捷查询单个元素，找不到返回 null。
 * @param {string} selector - CSS 选择器
 * @param {Element} [parent=document] - 父元素
 * @returns {Element|null}
 */
export function $(selector, parent = document) {
  return parent.querySelector(selector);
}

/**
 * 快捷查询所有匹配元素。
 * @param {string} selector - CSS 选择器
 * @param {Element} [parent=document] - 父元素
 * @returns {Element[]}
 */
export function $$(selector, parent = document) {
  return Array.from(parent.querySelectorAll(selector));
}

/**
 * HTML 转义，防止 XSS。用于纯文本需要插入 innerHTML 的场景。
 * @param {string} text - 待转义的文本
 * @returns {string} - 转义后的安全 HTML
 */
export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * 判断当前是否为移动端视口。
 * @returns {boolean}
 */
export function isMobile() {
  return window.innerWidth <= MOBILE_BREAKPOINT;
}

/**
 * 判断滚动容器是否已接近底部。
 * 用于"仅在用户本来就在底部时才自动跟随"的滚动策略，
 * 避免用户上翻查看历史时被强行拽回底部。
 * @param {Element} container - 滚动容器
 * @returns {boolean}
 */
export function isNearBottom(container) {
  if (!container) return true;
  return container.scrollTop + container.clientHeight
    >= container.scrollHeight - SCROLL_BOTTOM_THRESHOLD;
}

/**
 * 滚动容器到底部。
 * @param {Element} container - 滚动容器
 * @param {boolean} [force=false] - 为 true 时无视用户当前位置强制滚动
 */
export function scrollToBottom(container, force = false) {
  if (!container) return;
  if (force || isNearBottom(container)) {
    container.scrollTop = container.scrollHeight;
  }
}

/**
 * 创建一个带 Lucide 图标的按钮元素。
 * @param {string} iconName - Lucide 图标名（如 "copy", "check"）
 * @param {string} [title=""] - 按钮的 title 属性
 * @param {string} [className=""] - 附加的 CSS 类名
 * @returns {HTMLButtonElement}
 */
export function createIconButton(iconName, title = '', className = '') {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = className;
  btn.title = title;
  btn.innerHTML = `<i data-lucide="${iconName}"></i>`;
  return btn;
}

/**
 * 刷新 Lucide 图标（在动态插入 DOM 后调用）。
 */
export function refreshIcons() {
  if (window.lucide && window.lucide.createIcons) {
    window.lucide.createIcons();
  }
}

/**
 * 为 textarea 启用自动高度调整。
 *
 * 用户手动拖过高度后（元素带 data-manual-height），自动调整就让位——
 * 否则一打字就把用户拉好的高度重置回内容高度。
 *
 * @param {HTMLTextAreaElement} textarea - 目标 textarea
 * @param {number} [maxHeight=200] - 最大高度（像素）
 */
export function enableAutoResize(textarea, maxHeight = 200) {
  const resize = () => {
    if (textarea.dataset.manualHeight) return;
    textarea.style.height = 'auto';
    textarea.style.height = Math.min(textarea.scrollHeight, maxHeight) + 'px';
  };
  textarea.addEventListener('input', resize);
  resize();
}

/**
 * 简易防抖函数（尾调用）。
 * @param {Function} fn - 目标函数
 * @param {number} delay - 延迟毫秒数
 * @returns {Function} - 防抖后的函数
 */
export function debounce(fn, delay) {
  let timer = null;
  return function (...args) {
    clearTimeout(timer);
    timer = setTimeout(() => fn.apply(this, args), delay);
  };
}

/**
 * 简易节流函数（首次立即执行，后续节流）。
 * @param {Function} fn - 目标函数
 * @param {number} interval - 最小间隔毫秒数
 * @returns {Function} - 节流后的函数
 */
export function throttle(fn, interval) {
  let lastTime = 0;
  return function (...args) {
    const now = Date.now();
    if (now - lastTime >= interval) {
      lastTime = now;
      fn.apply(this, args);
    }
  };
}
