/**
 * history.js —— 侧边栏对话历史与分组
 *
 * 分组是纯前端能力：后端只存对话本身，分组关系放在 localStorage。
 * 渲染时把两份数据合并成"分组区 + 最近对话区"两段。
 */

import { $, $$, refreshIcons } from './dom-utils.js';
import * as api from './api.js';
import {
  state, getFolders, saveFolders, getFolderMap,
  addFolder, removeFolder, moveConversationToFolder,
} from './store.js';
import {
  showInputDialog, showConfirmDialog, showFolderPickerDialog, showContextMenu,
} from './dialogs.js';

/**
 * 创建单个历史条目。
 * @param {Object} conv - 对话摘要
 * @param {Object} handlers - 回调集合
 * @returns {HTMLElement}
 */
function createHistoryItem(conv, handlers) {
  const item = document.createElement('div');
  item.className = 'history-item' + (conv.id === state.currentConversationId ? ' active' : '');
  item.dataset.convId = conv.id;
  item.tabIndex = 0;
  item.setAttribute('role', 'button');

  const text = document.createElement('span');
  text.className = 'history-item-text';
  text.textContent = conv.title || '无标题';

  const more = document.createElement('span');
  more.className = 'history-item-more';
  more.title = '更多操作';
  more.innerHTML = '<i data-lucide="ellipsis"></i>';

  item.append(text, more);

  const open = () => handlers.onOpen(conv.id);
  item.addEventListener('click', (e) => {
    if (e.target.closest('.history-item-more')) return;
    open();
  });
  item.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); open(); }
  });

  more.addEventListener('click', (e) => {
    e.stopPropagation();
    e.preventDefault();
    openConversationMenu(more, conv, handlers);
  });

  return item;
}

/**
 * 弹出对话的右键菜单。
 * @param {HTMLElement} anchor - 菜单锚点
 * @param {Object} conv - 对话对象
 * @param {Object} handlers - 回调集合
 */
function openConversationMenu(anchor, conv, handlers) {
  const parent = anchor.closest('.history-item') || anchor.parentElement;
  showContextMenu(parent, [
    {
      icon: 'pencil',
      label: '重命名',
      onClick: async () => {
        const name = await showInputDialog({
          title: '重命名对话',
          placeholder: '请输入新的名称',
          defaultValue: conv.title || '无标题',
        });
        if (!name) return;
        try {
          await api.renameConversation(conv.id, name);
          handlers.onRefresh();
        } catch (err) {
          console.error('重命名失败:', err);
          handlers.onError('重命名失败: ' + err.message);
        }
      },
    },
    {
      icon: 'folder-input',
      label: '移动到分组',
      onClick: async () => {
        const folderId = await showFolderPickerDialog(conv);
        if (folderId === undefined) return;  // 用户取消
        moveConversationToFolder(conv.id, folderId);
        handlers.onRefresh();
      },
    },
    { separator: true },
    {
      icon: 'trash-2',
      label: '删除',
      danger: true,
      onClick: async () => {
        const ok = await showConfirmDialog({
          title: '删除对话',
          message: `确定删除"${conv.title || '无标题'}"吗？此操作不可撤销。`,
          confirmText: '删除',
        });
        if (!ok) return;
        try {
          await api.deleteConversation(conv.id);
          // 思考内容/解构卡片随消息在后端级联删除，
          // 无需再手动清理 localStorage（那里已不再存这些数据）
          if (conv.id === state.currentConversationId) handlers.onDeleteCurrent();
          handlers.onRefresh();
        } catch (err) {
          console.error('删除失败:', err);
          handlers.onError('删除失败: ' + err.message);
        }
      },
    },
  ]);
}

/**
 * 弹出分组的右键菜单。
 * @param {HTMLElement} folderEl - 分组元素
 * @param {Object} folder - 分组对象
 * @param {Object} handlers - 回调集合
 */
function openFolderMenu(folderEl, folder, handlers) {
  showContextMenu(folderEl, [
    {
      icon: 'pencil',
      label: '重命名分组',
      onClick: async () => {
        const name = await showInputDialog({
          title: '重命名分组',
          placeholder: '请输入新名称',
          defaultValue: folder.name,
        });
        if (!name) return;
        const folders = getFolders();
        const target = folders.find((f) => f.id === folder.id);
        if (target) { target.name = name; saveFolders(folders); }
        handlers.onRefresh();
      },
    },
    { separator: true },
    {
      icon: 'trash-2',
      label: '删除分组',
      danger: true,
      onClick: async () => {
        const ok = await showConfirmDialog({
          title: '删除分组',
          message: `确定删除分组"${folder.name}"吗？组内对话会移出分组，不会被删除。`,
          confirmText: '删除',
        });
        if (!ok) return;
        removeFolder(folder.id);
        handlers.onRefresh();
      },
    },
  ], { left: 'auto', right: '4px', top: '100%' });
}

/**
 * 创建一个分组节点（含其下的对话）。
 * @param {Object} folder - 分组对象
 * @param {Array} convs - 组内对话
 * @param {Object} handlers - 回调集合
 * @returns {HTMLElement}
 */
function createFolderNode(folder, convs, handlers) {
  const el = document.createElement('div');
  el.className = 'history-folder';
  el.dataset.folderId = folder.id;

  const arrow = document.createElement('i');
  arrow.className = 'folder-arrow';
  arrow.setAttribute('data-lucide', 'chevron-right');

  const icon = document.createElement('i');
  icon.className = 'folder-icon';
  icon.setAttribute('data-lucide', 'folder');

  const name = document.createElement('span');
  name.className = 'folder-name';
  name.textContent = folder.name;

  const count = document.createElement('span');
  count.className = 'folder-count';
  count.textContent = String(convs.length);

  const children = document.createElement('div');
  children.className = 'folder-children';
  convs.forEach((c) => children.appendChild(createHistoryItem(c, handlers)));

  el.append(arrow, icon, name, count, children);

  el.addEventListener('click', (e) => {
    if (e.target.closest('.folder-children') || e.target.closest('.history-item-more')) return;
    el.classList.toggle('open');
  });
  el.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    openFolderMenu(el, folder, handlers);
  });

  return el;
}

/**
 * 拉取并渲染完整的历史列表。
 * @param {HTMLElement} container - 历史区容器
 * @param {Object} handlers - 回调集合
 * @param {Function} handlers.onOpen - 打开对话
 * @param {Function} handlers.onRefresh - 请求重新渲染
 * @param {Function} handlers.onDeleteCurrent - 删除的是当前对话时的处理
 * @param {Function} handlers.onError - 错误提示
 */
export async function renderHistory(container, handlers) {
  let conversations;
  try {
    conversations = await api.listConversations();
  } catch (err) {
    console.error('加载历史失败:', err);
    container.innerHTML = '';
    const tip = document.createElement('div');
    tip.className = 'history-empty-hint';
    tip.textContent = '历史加载失败';
    container.appendChild(tip);
    return;
  }

  container.innerHTML = '';
  const folderMap = getFolderMap();
  const folders = getFolders();

  // 按分组归类
  const grouped = {};
  const ungrouped = [];
  conversations.forEach((conv) => {
    const fid = folderMap[conv.id];
    if (fid && folders.some((f) => f.id === fid)) {
      (grouped[fid] = grouped[fid] || []).push(conv);
    } else {
      ungrouped.push(conv);
    }
  });

  const frag = document.createDocumentFragment();

  // ── 分组区 ──
  const groupSection = document.createElement('div');
  groupSection.className = 'history-group-section';

  const groupHeader = document.createElement('div');
  groupHeader.className = 'history-group-header';

  const groupTitle = document.createElement('span');
  groupTitle.className = 'history-group-title';
  groupTitle.textContent = '对话分组';

  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'history-group-add-btn';
  addBtn.innerHTML = '<i data-lucide="plus"></i> 新分组';
  addBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const name = await showInputDialog({
      title: '新建分组',
      placeholder: '请输入分组名称',
      confirmText: '创建',
    });
    if (!name) return;
    addFolder(name);
    handlers.onRefresh();
  });

  groupHeader.append(groupTitle, addBtn);
  groupSection.appendChild(groupHeader);

  if (folders.length) {
    folders.forEach((folder) => {
      groupSection.appendChild(
        createFolderNode(folder, grouped[folder.id] || [], handlers),
      );
    });
  } else {
    const hint = document.createElement('div');
    hint.className = 'history-empty-hint';
    hint.textContent = '暂无分组';
    groupSection.appendChild(hint);
  }
  frag.appendChild(groupSection);

  // ── 最近对话区 ──
  const recentLabel = document.createElement('div');
  recentLabel.className = 'history-time-group';
  recentLabel.textContent = '最近对话';
  frag.appendChild(recentLabel);

  if (ungrouped.length) {
    ungrouped.forEach((conv) => frag.appendChild(createHistoryItem(conv, handlers)));
  } else {
    const hint = document.createElement('div');
    hint.className = 'history-empty-hint';
    hint.textContent = '暂无对话';
    frag.appendChild(hint);
  }

  container.appendChild(frag);
  refreshIcons();
}

/**
 * 高亮当前打开的对话条目。
 * @param {string|null} convId - 对话 ID
 */
export function highlightActive(convId) {
  $$('.history-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.convId === convId);
  });
}
