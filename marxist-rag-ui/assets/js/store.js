/**
 * store.js —— 应用状态与本地持久化
 *
 * 原来十个全局变量散落在 4000 行脚本里，谁都能改、改了没人知道。
 * 这里收成一个 state 对象加一组存取函数，并把 localStorage 的
 * try/catch 样板集中处理（隐私模式下 localStorage 会直接抛异常）。
 */

import {
  STORAGE_KEYS,
  THINKING_MAX_ENTRIES,
  THINKING_KEEP_ENTRIES,
} from './config.js';

// ── localStorage 安全读写 ──────────────────────────────────

/**
 * 读取并解析 localStorage 中的 JSON 值。
 * @param {string} key - 存储键
 * @param {*} fallback - 解析失败或不存在时的返回值
 * @returns {*}
 */
function readJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    // 隐私模式禁用存储，或历史数据格式损坏
    return fallback;
  }
}

/**
 * 将值序列化后写入 localStorage。
 * @param {string} key - 存储键
 * @param {*} value - 待存储的值
 * @returns {boolean} - 是否写入成功
 */
function writeJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    // 配额超限或存储被禁用，静默降级
    return false;
  }
}

/**
 * 读取布尔型开关。
 * @param {string} key - 存储键
 * @param {boolean} [fallback=false] - 默认值
 * @returns {boolean}
 */
function readBool(key, fallback = false) {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : raw === 'true';
  } catch {
    return fallback;
  }
}

/**
 * 写入布尔型开关。
 * @param {string} key - 存储键
 * @param {boolean} value - 开关值
 */
function writeBool(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    /* 忽略存储失败 */
  }
}

// ── 运行时状态 ──────────────────────────────────────────────

/**
 * 全局运行时状态。
 * 只读访问可直接取字段，写入请走下面的 setter，
 * 以便未来加订阅/日志时有统一入口。
 */
export const state = {
  /** 当前打开的对话 ID，null 表示新对话尚未落库 */
  currentConversationId: null,
  /** 是否正在流式生成 */
  isStreaming: false,
  /** 当前流的中断控制器 */
  abortController: null,
  /** 发起生成时所处的对话 ID，用于检测用户中途切换 */
  streamingConvId: null,
  /** 问答模式：general / methodology / original */
  currentMode: 'general',
  /** 深度思考开关（持久化） */
  thinkingMode: readBool(STORAGE_KEYS.thinkingMode, false),
  /** 联网搜索开关（持久化，原版漏了这个） */
  searchMode: readBool(STORAGE_KEYS.searchMode, false),
  /** 当前选中的模型 ID */
  currentModel: null,
  /** 可用模型列表 */
  availableModels: [],
  /** 最近一次提问，用于"重新生成" */
  lastQuestion: '',
};

/**
 * 判断用户是否在生成过程中切换到了别的对话。
 * 切换后不应再往界面写内容，但仍要继续消费流并累积文本。
 * @returns {boolean}
 */
export function isSwitchedAway() {
  return state.streamingConvId !== null
    && state.streamingConvId !== state.currentConversationId;
}

/**
 * 设置深度思考开关并持久化。
 * @param {boolean} enabled - 是否开启
 */
export function setThinkingMode(enabled) {
  state.thinkingMode = enabled;
  writeBool(STORAGE_KEYS.thinkingMode, enabled);
}

/**
 * 设置联网搜索开关并持久化。
 * @param {boolean} enabled - 是否开启
 */
export function setSearchMode(enabled) {
  state.searchMode = enabled;
  writeBool(STORAGE_KEYS.searchMode, enabled);
}

// ── 主题 ────────────────────────────────────────────────────

/**
 * 读取用户选择的主题。
 * @returns {string} - "light" | "dark" | "auto"
 */
export function getTheme() {
  try {
    return localStorage.getItem(STORAGE_KEYS.theme) || 'auto';
  } catch {
    return 'auto';
  }
}

/**
 * 保存主题偏好。
 * @param {string} theme - "light" | "dark" | "auto"
 */
export function saveTheme(theme) {
  try {
    localStorage.setItem(STORAGE_KEYS.theme, theme);
  } catch {
    /* 忽略 */
  }
}

// ── 对话分组（纯前端功能，后端不感知）────────────────────────

/**
 * 获取全部分组。
 * @returns {Array<{id: string, name: string}>}
 */
export function getFolders() {
  const list = readJSON(STORAGE_KEYS.folders, []);
  return Array.isArray(list) ? list : [];
}

/**
 * 保存分组列表。
 * @param {Array} folders - 分组数组
 */
export function saveFolders(folders) {
  writeJSON(STORAGE_KEYS.folders, folders);
}

/**
 * 获取"对话 ID → 分组 ID"的映射。
 * @returns {Object<string, string>}
 */
export function getFolderMap() {
  const map = readJSON(STORAGE_KEYS.convFolders, {});
  return (map && typeof map === 'object') ? map : {};
}

/**
 * 保存对话到分组的映射。
 * @param {Object} map - 映射对象
 */
export function saveFolderMap(map) {
  writeJSON(STORAGE_KEYS.convFolders, map);
}

/**
 * 新建一个分组。
 * @param {string} name - 分组名称
 * @returns {Object} - 新建的分组对象
 */
export function addFolder(name) {
  const folders = getFolders();
  const folder = { id: 'f_' + Date.now(), name };
  folders.push(folder);
  saveFolders(folders);
  return folder;
}

/**
 * 删除分组，同时清理映射中指向它的对话。
 * @param {string} folderId - 分组 ID
 */
export function removeFolder(folderId) {
  saveFolders(getFolders().filter((f) => f.id !== folderId));
  const map = getFolderMap();
  Object.keys(map).forEach((convId) => {
    if (map[convId] === folderId) delete map[convId];
  });
  saveFolderMap(map);
}

/**
 * 把对话移动到指定分组。
 * @param {string} convId - 对话 ID
 * @param {string|null} folderId - 目标分组 ID，null 表示移出分组
 */
export function moveConversationToFolder(convId, folderId) {
  const map = getFolderMap();
  if (folderId === null) {
    delete map[convId];
  } else {
    map[convId] = folderId;
  }
  saveFolderMap(map);
}

// ── 思考内容持久化 ──────────────────────────────────────────

/**
 * 保存某一轮回答的思考过程。
 *
 * 原实现用 querySelectorAll('.assistant-bubble') 数条数，
 * 但这个类名在项目里根本不存在，长度恒为 0，
 * 导致所有轮次都写进 stored[0] 互相覆盖。
 * 现在改为由调用方传入正确的序号。
 *
 * @param {string} convId - 对话 ID
 * @param {number} index - 这是该对话的第几条助手回答（从 0 开始）
 * @param {string} content - 思考过程文本
 */
export function saveThinkingContent(convId, index, content) {
  if (!convId || !content) return;
  const key = STORAGE_KEYS.thinkingPrefix + convId;
  const stored = readJSON(key, {});
  stored[String(index)] = content;

  // 超长对话时裁掉最早的记录，防止 localStorage 配额被撑爆
  const keys = Object.keys(stored);
  if (keys.length > THINKING_MAX_ENTRIES) {
    keys.sort((a, b) => parseInt(a, 10) - parseInt(b, 10));
    keys.slice(0, keys.length - THINKING_KEEP_ENTRIES)
      .forEach((k) => delete stored[k]);
  }
  writeJSON(key, stored);
}

/**
 * 读取某个对话保存的全部思考过程。
 * @param {string} convId - 对话 ID
 * @returns {Object<string, string>} - { "0": "...", "1": "..." }
 */
export function getThinkingContents(convId) {
  return readJSON(STORAGE_KEYS.thinkingPrefix + convId, {});
}

/**
 * 删除对话时一并清理它的思考记录。
 * @param {string} convId - 对话 ID
 */
export function clearThinkingContents(convId) {
  try {
    localStorage.removeItem(STORAGE_KEYS.thinkingPrefix + convId);
  } catch {
    /* 忽略 */
  }
}
