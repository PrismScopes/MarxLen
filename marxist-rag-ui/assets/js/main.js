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
  state, setThinkingEffort, applyDefaultThinkingEffort,
  setSearchMode,
} from './store.js';
import { initMarkdown } from './markdown.js';
import { initTheme } from './theme.js';
import { initDialogGlobalHandlers, showInputDialog, showConfirmDialog } from './dialogs.js';
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
  modelModalOverlay: $('#modelModalOverlay'),
  modelModalCloseBtn: $('#modelModalCloseBtn'),
  modelModalModelList: $('#modelModalModelList'),
  modelModalEffortList: $('#modelModalEffortList'),
  modelModalSettingsBtn: $('#modelModalSettingsBtn'),
  modeDescription: $('#modeDescription'),
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
    // 模型管理卡里增删模型后,刷新主界面的模型下拉
    () => loadModels(),
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

// ── 模型与推理等级弹窗(参考 DSH 的模型选择弹窗) ─────────────

/** 思考强度的展示文案(与 DSH 推理等级一致) */
const EFFORT_LABELS = {
  off: { label: '关闭思考', short: 'Off', desc: '不启用推理，秒回' },
  high: { label: '标准思考', short: 'High', desc: '标准推理强度' },
  max: { label: '深度思考', short: 'Max', desc: '深度推理，耗时较长' },
};
const EFFORT_ORDER = ['off', 'high', 'max'];

/**
 * 刷新触发器文案:"模型名 · 档位"组合(如 deepseek-v4-pro · Max)。
 */
function updateModelSelectorLabel() {
  const model = state.availableModels.find((m) => m.id === state.currentModel);
  const name = model ? model.name : (state.currentModel || '加载中...');
  const effort = EFFORT_LABELS[state.thinkingEffort] || EFFORT_LABELS.off;
  refs.modelSelectorName.textContent = `${name} · ${effort.short}`;
}

/**
 * 渲染弹窗内容:「模型」与「推理等级」两个面板。
 */
function renderModelModal() {
  // ── 模型组 ──
  refs.modelModalModelList.innerHTML = '';
  state.availableModels.forEach((model) => {
    const active = model.id === state.currentModel;
    const item = document.createElement('div');
    item.className = 'model-modal-option' + (active ? ' active' : '');
    item.setAttribute('role', 'option');
    item.setAttribute('aria-selected', active ? 'true' : 'false');

    const stack = document.createElement('div');
    stack.className = 'model-modal-option-stack';
    const name = document.createElement('div');
    name.className = 'model-modal-option-name';
    name.textContent = model.name;
    stack.appendChild(name);
    if (model.provider && model.provider !== 'DeepSeek') {
      const desc = document.createElement('div');
      desc.className = 'model-modal-option-desc';
      desc.textContent = model.provider;
      stack.appendChild(desc);
    }
    item.appendChild(stack);

    const check = document.createElement('i');
    check.setAttribute('data-lucide', 'check');
    check.className = 'model-modal-check';
    item.appendChild(check);

    item.addEventListener('click', () => selectModel(model));
    refs.modelModalModelList.appendChild(item);
  });

  // ── 添加自定义模型入口 ──
  const addItem = document.createElement('div');
  addItem.className = 'model-modal-add';
  addItem.setAttribute('role', 'button');
  addItem.textContent = '＋ 添加自定义模型';
  addItem.title = '直接输入模型 ID 添加，无需编辑配置文件';
  addItem.addEventListener('click', () => addCustomModel());
  refs.modelModalModelList.appendChild(addItem);

  // ── 推理等级组 ──
  refs.modelModalEffortList.innerHTML = '';
  EFFORT_ORDER.forEach((effort) => {
    const meta = EFFORT_LABELS[effort];
    const active = effort === state.thinkingEffort;
    const item = document.createElement('div');
    item.className = 'model-modal-option' + (active ? ' active' : '');
    item.setAttribute('role', 'option');
    item.setAttribute('aria-selected', active ? 'true' : 'false');

    const stack = document.createElement('div');
    stack.className = 'model-modal-option-stack';
    const name = document.createElement('div');
    name.className = 'model-modal-option-name';
    name.textContent = meta.label;
    const desc = document.createElement('div');
    desc.className = 'model-modal-option-desc';
    desc.textContent = meta.desc;
    stack.append(name, desc);
    item.appendChild(stack);

    const check = document.createElement('i');
    check.setAttribute('data-lucide', 'check');
    check.className = 'model-modal-check';
    item.appendChild(check);

    item.addEventListener('click', () => selectEffort(effort));
    refs.modelModalEffortList.appendChild(item);
  });

  refreshIcons();
}

/**
 * 打开 / 关闭模型弹窗。
 */
function openModelModal() {
  renderModelModal();
  refs.modelModalOverlay.classList.add('open');
  refs.modelSelector.setAttribute('aria-expanded', 'true');
}

function closeModelModal() {
  refs.modelModalOverlay.classList.remove('open');
  refs.modelSelector.setAttribute('aria-expanded', 'false');
}

/**
 * 选中某个模型并持久化到后端(弹窗保持打开,便于继续调档位)。
 * @param {Object} model - 模型对象
 */
async function selectModel(model) {
  state.currentModel = model.id;
  updateModelSelectorLabel();
  renderModelModal();
  try {
    await api.saveModel(model.id);
  } catch (err) {
    console.error('保存模型设置失败:', err);
  }
}

/**
 * 选中某个推理等级(即时生效,弹窗保持打开)。
 * @param {string} effort - off / high / max
 */
function selectEffort(effort) {
  setThinkingEffort(effort);
  updateModelSelectorLabel();
  renderModelModal();
  updateModeDescription();
}

/**
 * 在 GUI 中直接添加自定义模型(快捷方式)。
 *
 * 与设置页的模型管理共用同一后端端点 POST /api/models:
 * 对话框填模型 ID 与显示名 → 后端追加到 OPENAI_MODEL_LIST
 * (重复 id 自动更新显示名)→ 重新拉取模型列表 →
 * 新模型立即出现在弹窗列表并自动选中。
 */
async function addCustomModel() {
  const result = await showInputDialog({
    title: '添加自定义模型',
    fields: [
      { key: 'id', label: '模型 ID', placeholder: '如 deepseek-v4-flash' },
      { key: 'name', label: '显示名', placeholder: '如 DeepSeek-V4-Flash' },
    ],
    confirmText: '添加',
  });
  if (!result || !result.id) return;

  const modelId = result.id.trim();
  const displayName = result.name.trim() || modelId;

  // 已存在:直接选中,不重复添加
  if (state.availableModels.some((m) => m.id === modelId)) {
    selectModel({ id: modelId, name: displayName });
    return;
  }

  try {
    await api.addModel(modelId, displayName);
    // 后端已写入配置,重新拉取模型列表并选中新模型
    await loadModels();
    selectModel({ id: modelId, name: displayName });
  } catch (err) {
    console.error('添加模型失败:', err);
    showConfirmDialog({
      title: '添加模型失败',
      message: err.message || String(err),
      confirmText: '知道了',
    });
  }
}

/**
 * 拉取模型列表，失败时用兜底模型。
 * 优先恢复后端记录的当前生效模型(current)——用户切换过模型后
 * 刷新页面也能保持选择,而不是默认选列表第一个。
 * 后端没记 current 或 current 不在列表时,回退保留原选择;
 * 再不行才选列表第一个。
 */
async function loadModels() {
  const oldModel = state.currentModel;
  let backendCurrent = null;
  let models = [];
  try {
    const data = await api.listModels();
    models = data.models || [];
    backendCurrent = data.current || null;
  } catch (err) {
    console.error('加载模型列表失败:', err);
  }
  state.availableModels = models.length ? models : [FALLBACK_MODEL];

  if (backendCurrent && state.availableModels.some((m) => m.id === backendCurrent)) {
    state.currentModel = backendCurrent;
  } else if (oldModel && state.availableModels.some((m) => m.id === oldModel)) {
    state.currentModel = oldModel;
  } else {
    const first = state.availableModels[0];
    state.currentModel = first ? first.id : null;
  }
  updateModelSelectorLabel();
  renderModelModal();
}

/**
 * 绑定模型弹窗的开关与键盘行为。
 */
function initModelSelector() {
  refs.modelSelector.addEventListener('click', (e) => {
    e.stopPropagation();
    if (refs.modelModalOverlay.classList.contains('open')) {
      closeModelModal();
    } else {
      openModelModal();
    }
  });
  refs.modelSelector.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openModelModal();
    }
  });

  // 关闭按钮 / 遮罩点击 / Esc
  refs.modelModalCloseBtn.addEventListener('click', closeModelModal);
  refs.modelModalOverlay.addEventListener('click', (e) => {
    if (e.target === refs.modelModalOverlay) closeModelModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && refs.modelModalOverlay.classList.contains('open')) {
      closeModelModal();
    }
  });

  // 从弹窗直达设置页(管理模型列表/默认思考强度)
  refs.modelModalSettingsBtn.addEventListener('click', () => {
    closeModelModal();
    openSettings();
  });
}

// ── 模式与开关 ──────────────────────────────────────────────

/**
 * 刷新输入框上方的模式说明文案。
 */
function updateModeDescription() {
  const base = MODE_DESCRIPTIONS[state.currentMode] || MODE_DESCRIPTIONS.general;
  const tags = [];
  if (state.thinkingEffort === 'max') tags.push('深度思考');
  else if (state.thinkingEffort === 'high') tags.push('标准思考');
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
 * 绑定联网搜索开关。
 *
 * 思考强度不再有独立按钮:与模型合并在模型弹窗的"推理等级"面板
 * (见 renderModelModal / selectEffort,交互与 DSH 一致)。
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

  sync(refs.searchToggle, state.searchMode, '关闭联网搜索', '联网搜索');

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
    // 思考内容与解构卡片随消息从后端返回，无需再传 localStorage 数据
    chat.renderConversation(convId, detail.messages || []);
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
  // 应用后端配置的默认思考强度(仅当本地无记录时),刷新组合触发器
  try {
    const settings = await api.getSettings();
    const item = (settings.items || []).find((i) => i.key === 'thinking_effort');
    if (item) {
      applyDefaultThinkingEffort(item.value);
      updateModelSelectorLabel();
      renderModelModal();
    }
  } catch (err) {
    console.warn('读取默认思考强度失败:', err);
  }
  refreshHistory();
}

// DOM 就绪后启动（模块脚本天然 defer，这里再兜一层）
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
