/**
 * main.js —— 应用入口
 *
 * 只做三件事：拿 DOM 引用、把各模块接起来、绑定顶层事件。
 * 具体逻辑���在各自模块里，这里保持薄。
 */

import { $, $$, refreshIcons, isMobile, enableAutoResize } from './dom-utils.js';
import { MODE_DESCRIPTIONS, TEXTAREA_MAX_HEIGHT, FALLBACK_MODEL } from './config.js';
import * as api from './api.js';
import {
  state, setThinkingMode, setSearchMode, getThinkingContents,
} from './store.js';
import { initMarkdown } from './markdown.js';
import { initTheme } from './theme.js';
import { initDialogGlobalHandlers } from './dialogs.js';
import { updateTimeline, highlightTimeline } from './messages.js';
import { renderHistory, highlightActive } from './history.js';
import { loadSettings } from './settings.js';
import { createChatController } from './chat.js';
import { initReader, openReader, closeReader } from './reader.js';
import { initQuoteBar } from './quote.js';

// ── DOM 引用 ────────────────────────────────────────────────

const refs = {
  sidebar: $('#chatSidebar'),
  sidebarOverlay: $('#sidebarOverlay'),
  sidebarToggle: $('#sidebarToggle'),
  hamburger: $('#hamburgerBtn'),
  themeToggle: $('#themeToggle'),
  chatMessages: $('#chatMessages'),
  vTimeline: $('#vTimeline'),
  welcomeContainer: $('#welcomeContainer'),
  conversationContainer: $('#conversationContainer'),
  textarea: $('#chatTextarea'),
  sendBtn: $('#sendBtn'),
  stopBtn: $('#stopBtn'),
  historySection: $('#historySection'),
  newChatBtn: $('#newChatBtn'),
  modelSelector: $('#modelSelector'),
  modelSelectorName: $('#modelSelectorName'),
  modelDropdown: $('#modelDropdown'),
  modeDescription: $('#modeDescription'),
  thinkingToggle: $('#thinkingToggle'),
  searchToggle: $('#searchToggle'),
  settingsOverlay: $('#settingsOverlay'),
  settingsPanelBody: $('#settingsPanelBody'),
  settingsCloseBtn: $('#settingsCloseBtn'),
  settingsBtn: $('#sidebarSettingsBtn'),
  settingsNav: $('#settingsNav'),
  settingsContentTitle: $('#settingsContentTitle'),
  settingsContentDesc: $('#settingsContentDesc'),
};

/** 对话大纲的节点映射，滚动时用来高亮 */
let timelineDots = [];

// ── 侧边栏 ──────────────────────────────────────────────────

/**
 * 打开移动端侧边栏抽屉。
 */
function openSidebar() {
  refs.sidebar.classList.add('open');
  refs.sidebarOverlay.classList.add('active', 'visible');
}

/**
 * 关闭移动端侧边栏抽屉。
 */
function closeSidebar() {
  refs.sidebar.classList.remove('open');
  refs.sidebarOverlay.classList.remove('visible');
  // 等淡出动画结束再移除 active，否则遮罩会瞬间消失
  setTimeout(() => refs.sidebarOverlay.classList.remove('active'), 250);
}

/**
 * 绑定侧边栏的折叠与抽屉行为。
 */
function initSidebar() {
  refs.hamburger.addEventListener('click', openSidebar);
  refs.sidebarOverlay.addEventListener('click', closeSidebar);

  let collapsed = false;
  refs.sidebarToggle.addEventListener('click', () => {
    collapsed = !collapsed;
    refs.sidebar.classList.toggle('collapsed', collapsed);
    refs.sidebarToggle.classList.toggle('collapsed', collapsed);
    const label = collapsed ? '展开侧边栏' : '收起侧边栏';
    refs.sidebarToggle.title = label;
    refs.sidebarToggle.setAttribute('aria-label', label);
    refs.sidebarToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    refs.sidebarToggle.innerHTML = collapsed
      ? '<i data-lucide="panel-left-open"></i>'
      : '<i data-lucide="panel-left-close"></i>';
    refreshIcons();
  });
}

// ── 设置面板 ────────────────────────────────────────────────

/**
 * 打开设置面板并加载内容。
 */
function openSettings() {
  refs.settingsOverlay.classList.add('open');
  loadSettings(
    {
      nav: refs.settingsNav,
      body: refs.settingsPanelBody,
      title: refs.settingsContentTitle,
      desc: refs.settingsContentDesc,
    },
    (modelId) => {
      const model = state.availableModels.find((m) => m.id === modelId);
      if (model) selectModel(model);
    },
    state.availableModels,
  );
}

/**
 * 关闭设置面板。
 */
function closeSettings() {
  refs.settingsOverlay.classList.remove('open');
}

/**
 * 绑定设置面板的开关事件。
 */
function initSettingsPanel() {
  refs.settingsBtn.addEventListener('click', () => {
    openSettings();
    if (isMobile()) closeSidebar();
  });
  refs.settingsCloseBtn.addEventListener('click', closeSettings);
  refs.settingsOverlay.addEventListener('click', (e) => {
    if (e.target === refs.settingsOverlay) closeSettings();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeSettings();
  });
}

// ── 模型选择 ────────────────────────────────────────────────

/**
 * 渲染模型下拉列表。
 */
function renderModelDropdown() {
  refs.modelDropdown.innerHTML = '';
  state.availableModels.forEach((model) => {
    const item = document.createElement('div');
    item.className = 'model-dropdown-item' + (model.id === state.currentModel ? ' active' : '');
    item.setAttribute('role', 'option');
    item.setAttribute('aria-selected', model.id === state.currentModel ? 'true' : 'false');
    item.textContent = model.name;
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      selectModel(model);
    });
    refs.modelDropdown.appendChild(item);
  });
}

/**
 * 选中某个模型并持久化到后端。
 * @param {Object} model - 模型对象
 */
async function selectModel(model) {
  state.currentModel = model.id;
  refs.modelSelectorName.textContent = model.name;
  refs.modelDropdown.classList.remove('open');
  refs.modelSelector.setAttribute('aria-expanded', 'false');
  renderModelDropdown();
  try {
    await api.saveModel(model.id);
  } catch (err) {
    console.error('保存模型设置失败:', err);
  }
}

/**
 * 拉取模型列表，失败时用兜底模型。
 */
async function loadModels() {
  try {
    const models = await api.listModels();
    state.availableModels = models.length ? models : [FALLBACK_MODEL];
  } catch (err) {
    console.error('加载模型列表失败:', err);
    state.availableModels = [FALLBACK_MODEL];
  }
  const first = state.availableModels[0];
  state.currentModel = first.id;
  refs.modelSelectorName.textContent = first.name;
  renderModelDropdown();
}

/**
 * 绑定模型选择器的展开/收起。
 */
function initModelSelector() {
  const toggle = () => {
    const open = refs.modelDropdown.classList.toggle('open');
    refs.modelSelector.setAttribute('aria-expanded', open ? 'true' : 'false');
  };

  refs.modelSelector.addEventListener('click', (e) => {
    e.stopPropagation();
    toggle();
  });
  refs.modelSelector.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    if (e.key === 'Escape') {
      refs.modelDropdown.classList.remove('open');
      refs.modelSelector.setAttribute('aria-expanded', 'false');
    }
  });
  // 点击别处收起
  document.addEventListener('click', () => {
    refs.modelDropdown.classList.remove('open');
    refs.modelSelector.setAttribute('aria-expanded', 'false');
  });
}

// ── 模式与开关 ──────────────────────────────────────────────

/**
 * 刷新输入框上方的模式说明文案。
 */
function updateModeDescription() {
  const base = MODE_DESCRIPTIONS[state.currentMode] || MODE_DESCRIPTIONS.general;
  const tags = [];
  if (state.thinkingMode) tags.push('深度思考');
  if (state.searchMode) tags.push('联网搜索');
  refs.modeDescription.textContent = tags.length ? `${base}（${tags.join(' + ')}）` : base;
}

/**
 * 绑定三种问答模式的切换。
 */
function initModeSwitcher() {
  $$('.mode-tab').forEach((tab) => {
    const activate = () => {
      $$('.mode-tab').forEach((t) => {
        t.classList.remove('active');
        t.setAttribute('aria-selected', 'false');
      });
      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
      state.currentMode = tab.dataset.mode;
      updateModeDescription();

      // 原文查询不是问答，选中即切到阅读器视图；
      // 切回其他模式时要退出阅读器，否则输入框一直被藏着。
      if (state.currentMode === 'original') {
        openReader();
      } else {
        closeReader();
      }
    };
    tab.addEventListener('click', activate);
    tab.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
    });
  });
}

/**
 * 绑定深度思考与联网搜索开关。
 */
function initFeatureToggles() {
  /**
   * 同步一个开关按钮的视觉状态。
   * @param {HTMLElement} btn - 按钮
   * @param {boolean} active - 是否激活
   * @param {string} onLabel - 激活时的提示
   * @param {string} offLabel - 关闭时的提示
   */
  const sync = (btn, active, onLabel, offLabel) => {
    btn.classList.toggle('active', active);
    btn.title = active ? onLabel : offLabel;
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  };

  sync(refs.thinkingToggle, state.thinkingMode, '关闭深度思考', '深度思考模式');
  sync(refs.searchToggle, state.searchMode, '关闭联网搜索', '联网搜索');

  refs.thinkingToggle.addEventListener('click', () => {
    setThinkingMode(!state.thinkingMode);
    sync(refs.thinkingToggle, state.thinkingMode, '关闭深度思考', '深度思考模式');
    updateModeDescription();
  });

  refs.searchToggle.addEventListener('click', () => {
    setSearchMode(!state.searchMode);
    sync(refs.searchToggle, state.searchMode, '关闭联网搜索', '联网搜索');
    updateModeDescription();
  });
}

// ── 组装 ────────────────────────────────────────────────────

/**
 * 重建对话大纲。
 */
function refreshTimeline() {
  timelineDots = updateTimeline(
    refs.vTimeline, refs.conversationContainer, refs.chatMessages,
  );
}

/** 聊天控制器实例 */
const chat = createChatController(refs, {
  onTimelineUpdate: refreshTimeline,
  onHistoryRefresh: () => refreshHistory(),
});

/**
 * 重新拉取并渲染历史列表。
 */
function refreshHistory() {
  renderHistory(refs.historySection, {
    onOpen: openConversation,
    onRefresh: refreshHistory,
    onDeleteCurrent: () => chat.newChat(),
    onError: (msg) => console.error(msg),
  });
}

/**
 * 打开一个历史对话。
 * @param {string} convId - 对话 ID
 */
async function openConversation(convId) {
  try {
    const detail = await api.getConversation(convId);
    chat.renderConversation(convId, getThinkingContents(convId), detail.messages || []);
    highlightActive(convId);
    if (isMobile()) closeSidebar();
  } catch (err) {
    console.error('加载对话失败:', err);
  }
}

/**
 * 绑定输入框相关事件。
 */
function initInput() {
  enableAutoResize(refs.textarea, TEXTAREA_MAX_HEIGHT);
  initQuoteBar();
  initResizeHandle();

  refs.textarea.addEventListener('input', () => {
    if (!state.isStreaming) refs.sendBtn.disabled = !refs.textarea.value.trim();
  });

  refs.textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      chat.send();
    }
  });

  refs.sendBtn.addEventListener('click', () => chat.send());
  refs.stopBtn.addEventListener('click', () => chat.stop());
  refs.newChatBtn.addEventListener('click', () => {
    chat.newChat();
    highlightActive(null);
    if (isMobile()) closeSidebar();
  });
}

/**
 * 输入框高度拖拽。
 *
 * 自动增高有个上限，粘进来一大段就只能在很小的窗口里滚动，
 * 看不清全貌。这里允许手动把输入框拉高。
 */
function initResizeHandle() {
  const handle = $('#chatResizeHandle');
  if (!handle) return;

  let startY = 0;
  let startH = 0;

  const onMove = (e) => {
    // 往上拖变高、往下拖收缩：手柄在输入框顶部，
    // 拖动方向和输入框上边界的移动方向一致才符合直觉，
    // 所以是 startY - clientY
    const next = Math.max(36, startH + (startY - e.clientY));
    // 同时写 maxHeight：CSS 里的 45vh 上限会盖住 height，
    // 只设 height 的话拖过 45vh 就没反应了
    refs.textarea.style.height = `${next}px`;
    refs.textarea.style.maxHeight = `${next}px`;
    // 打上标记，让 enableAutoResize 不要再按内容覆盖这个高度
    refs.textarea.dataset.manualHeight = '1';
  };

  const onUp = () => {
    handle.classList.remove('dragging');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    // 拖拽期间禁掉选中，否则会把页面文字一起刷蓝
    document.body.style.userSelect = '';
  };

  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    startY = e.clientY;
    startH = refs.textarea.offsetHeight;
    handle.classList.add('dragging');
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // 双击手柄复位，省得用户拖高了要一点点拖回来
  handle.addEventListener('dblclick', () => {
    refs.textarea.style.height = '';
    refs.textarea.style.maxHeight = '';
    // 交还给自动增高，否则复位后高度就再也不跟内容走了
    delete refs.textarea.dataset.manualHeight;
    refs.textarea.dispatchEvent(new Event('input'));
  });
}

/**
 * 等待第三方库加载完成。
 *
 * type="module" 的脚本和 defer 脚本虽然都在 DOMContentLoaded 前执行，
 * 但两者的相对顺序没有保证，模块可能先于 lucide/marked/DOMPurify 跑起来。
 * 这里轮询等待，超时后也继续启动（各模块内部都有降级处理）。
 *
 * @param {number} [timeoutMs=5000] - 最长等待时间
 * @returns {Promise<void>}
 */
function waitForLibraries(timeoutMs = 5000) {
  const ready = () => window.lucide && window.marked && window.DOMPurify;
  if (ready()) return Promise.resolve();

  return new Promise((resolve) => {
    const start = Date.now();
    const timer = setInterval(() => {
      if (ready() || Date.now() - start > timeoutMs) {
        clearInterval(timer);
        if (!ready()) {
          console.warn('[main] 第三方库加载超时，部分功能可能降级');
        }
        resolve();
      }
    }, 20);
  });
}

/**
 * 应用启动。
 */
async function init() {
  await waitForLibraries();

  initMarkdown();
  refreshIcons();
  initTheme(refs.themeToggle);
  initSidebar();
  initSettingsPanel();
  initModelSelector();
  initModeSwitcher();
  initFeatureToggles();
  initInput();
  initDialogGlobalHandlers();
  initReader();

  updateModeDescription();
  refs.sendBtn.disabled = true;

  // 滚动时高亮大纲当前节点
  refs.chatMessages.addEventListener('scroll', () => {
    highlightTimeline(timelineDots, refs.chatMessages);
  });

  await loadModels();
  refreshHistory();
}

// DOM 就绪后启动（模块脚本天然 defer，这里再兜一层）
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
