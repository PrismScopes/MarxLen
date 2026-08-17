/**
 * dialogs.js —— 自定义对话框与右键菜单
 *
 * 替代原生 prompt/confirm，保持视觉统一。
 * 全部用 createElement + textContent 构建，不拼 innerHTML，
 * 因此分组名等用户输入天然免疫注入。
 */

import { $, $$, refreshIcons } from './dom-utils.js';
import { getFolders, getFolderMap } from './store.js';

/** 当前打开的右键菜单，同一时刻只允许一个 */
let activeContextMenu = null;

/**
 * 关闭当前右键菜单。
 */
export function closeContextMenu() {
  if (activeContextMenu) {
    activeContextMenu.remove();
    activeContextMenu = null;
  }
}

/**
 * 构建一个遮罩 + 对话框骨架。
 * @param {string} titleText - 标题文本
 * @returns {{overlay: HTMLElement, body: HTMLElement, footer: HTMLElement, close: Function}}
 */
function buildDialogShell(titleText) {
  const overlay = document.createElement('div');
  overlay.className = 'inline-dialog-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');

  const dialog = document.createElement('div');
  dialog.className = 'inline-dialog';

  const header = document.createElement('div');
  header.className = 'inline-dialog-header';
  header.textContent = titleText;

  const body = document.createElement('div');
  body.className = 'inline-dialog-body';

  const footer = document.createElement('div');
  footer.className = 'inline-dialog-footer';

  dialog.append(header, body, footer);
  overlay.appendChild(dialog);

  const close = () => overlay.remove();
  return { overlay, body, footer, close };
}

/**
 * 创建对话框底部的取消/确定按钮。
 * @param {string} confirmText - 确定按钮文案
 * @param {Function} onCancel - 取消回调
 * @param {Function} onConfirm - 确定回调
 * @returns {{cancelBtn: HTMLButtonElement, confirmBtn: HTMLButtonElement}}
 */
function buildDialogButtons(confirmText, onCancel, onConfirm) {
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'inline-dialog-btn cancel';
  cancelBtn.textContent = '取消';
  cancelBtn.addEventListener('click', onCancel);

  const confirmBtn = document.createElement('button');
  confirmBtn.type = 'button';
  confirmBtn.className = 'inline-dialog-btn confirm';
  confirmBtn.textContent = confirmText;
  confirmBtn.addEventListener('click', onConfirm);

  return { cancelBtn, confirmBtn };
}

/**
 * 弹出文本输入对话框。
 *
 * 支持两种形态:
 *   - 单个输入框:不传 fields,resolve 返回字符串
 *   - 多字段表单:传 options.fields = [{key, label, placeholder, defaultValue}],
 *     resolve 返回 { key: value, ... }
 *
 * @param {Object} options - 配置
 * @param {string} options.title - 标题
 * @param {string} [options.placeholder] - 单输入框占位符
 * @param {string} [options.defaultValue] - 单输入框默认值
 * @param {Array} [options.fields] - 多字段表单定义
 * @param {string} [options.confirmText] - 确定按钮文案
 * @returns {Promise<string|Object|null>} - 输入内容，取消时为 null
 */
export function showInputDialog(options) {
  return new Promise((resolve) => {
    const { overlay, body, footer, close } = buildDialogShell(options.title || '请输入');
    const fields = Array.isArray(options.fields) && options.fields.length
      ? options.fields : null;
    const inputs = [];

    const finish = (value) => {
      close();
      document.removeEventListener('keydown', onKeydown);
      resolve(value);
    };

    if (fields) {
      fields.forEach((f) => {
        if (f.label) {
          const lab = document.createElement('div');
          lab.className = 'inline-dialog-field-label';
          lab.textContent = f.label;
          body.appendChild(lab);
        }
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'inline-dialog-input';
        input.placeholder = f.placeholder || '';
        input.value = f.defaultValue || '';
        input.setAttribute('aria-label', f.label || f.placeholder || '输入');
        body.appendChild(input);
        inputs.push(input);
      });
    } else {
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'inline-dialog-input';
      input.placeholder = options.placeholder || '';
      input.value = options.defaultValue || '';
      input.setAttribute('aria-label', options.title || '输入');
      body.appendChild(input);
      inputs.push(input);
    }

    const { cancelBtn, confirmBtn } = buildDialogButtons(
      options.confirmText || '确定',
      () => finish(null),
      () => {
        if (fields) {
          const values = {};
          fields.forEach((f, i) => { values[f.key] = inputs[i].value.trim(); });
          finish(Object.values(values).some((v) => v) ? values : null);
        } else {
          const value = inputs[0].value.trim();
          finish(value || null);
        }
      },
    );
    footer.append(cancelBtn, confirmBtn);

    /**
     * 键盘快捷键：Enter 前进/确认，Escape 取消。
     * @param {KeyboardEvent} e
     */
    function onKeydown(e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        const idx = inputs.indexOf(document.activeElement);
        if (idx >= 0 && idx < inputs.length - 1) {
          inputs[idx + 1].focus();
        } else {
          confirmBtn.click();
        }
      }
      if (e.key === 'Escape') { e.preventDefault(); cancelBtn.click(); }
    }
    inputs.forEach((input) => input.addEventListener('keydown', onKeydown));

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) cancelBtn.click();
    });

    document.body.appendChild(overlay);
    inputs[0].focus();
    inputs[0].select();
  });
}

/**
 * 弹出确认对话框。
 * @param {Object} options - 配置
 * @param {string} options.title - 标题
 * @param {string} options.message - 提示内容
 * @param {string} [options.confirmText] - 确定按钮文案
 * @returns {Promise<boolean>} - 是否确认
 */
export function showConfirmDialog(options) {
  return new Promise((resolve) => {
    const { overlay, body, footer, close } = buildDialogShell(options.title || '确认');

    const msg = document.createElement('p');
    msg.className = 'inline-dialog-message';
    msg.textContent = options.message || '';
    body.appendChild(msg);

    const finish = (value) => { close(); resolve(value); };
    const { cancelBtn, confirmBtn } = buildDialogButtons(
      options.confirmText || '确定',
      () => finish(false),
      () => finish(true),
    );
    footer.append(cancelBtn, confirmBtn);

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) finish(false);
    });

    document.body.appendChild(overlay);
    confirmBtn.focus();
  });
}

/**
 * 弹出分组选择对话框。
 * @param {Object} conv - 对话对象，需含 id
 * @returns {Promise<string|null|undefined>} - 选中的分组 ID；null 表示移出分组；undefined 表示取消
 */
export function showFolderPickerDialog(conv) {
  return new Promise((resolve) => {
    const folders = getFolders();
    const folderMap = getFolderMap();
    let selected = folderMap[conv.id] || null;

    const { overlay, body, footer, close } = buildDialogShell('移动到分组');

    const list = document.createElement('div');
    list.className = 'folder-picker-list';
    list.setAttribute('role', 'listbox');

    /**
     * 创建一个可选项。
     * @param {string} iconName - Lucide 图标名
     * @param {string} label - 显示文本
     * @param {string|null} value - 对应的分组 ID
     * @returns {HTMLElement}
     */
    const createItem = (iconName, label, value) => {
      const item = document.createElement('div');
      item.className = 'folder-picker-item' + (selected === value ? ' selected' : '');
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', selected === value ? 'true' : 'false');

      const icon = document.createElement('i');
      icon.setAttribute('data-lucide', iconName);
      const text = document.createElement('span');
      text.textContent = label;
      item.append(icon, text);

      item.addEventListener('click', () => {
        $$('.folder-picker-item', list).forEach((el) => {
          el.classList.remove('selected');
          el.setAttribute('aria-selected', 'false');
        });
        item.classList.add('selected');
        item.setAttribute('aria-selected', 'true');
        selected = value;
      });
      return item;
    };

    list.appendChild(createItem('corner-up-left', '无分组', null));
    folders.forEach((f) => list.appendChild(createItem('folder', f.name, f.id)));
    body.appendChild(list);

    const finish = (value) => { close(); resolve(value); };
    const { cancelBtn, confirmBtn } = buildDialogButtons(
      '确定',
      () => finish(undefined),
      () => finish(selected),
    );
    footer.append(cancelBtn, confirmBtn);

    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) finish(undefined);
    });

    document.body.appendChild(overlay);
    refreshIcons();
  });
}

/**
 * 在指定元素上显示右键菜单。
 * @param {Element} anchorEl - 菜单挂载的父元素（需 position: relative）
 * @param {Array<{icon: string, label: string, danger?: boolean, onClick: Function}>} items - 菜单项
 * @param {Object} [style] - 可选的定位覆盖，如 { right: '4px', top: '100%' }
 */
export function showContextMenu(anchorEl, items, style) {
  closeContextMenu();

  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  menu.setAttribute('role', 'menu');
  if (style) Object.assign(menu.style, style);

  items.forEach((cfg) => {
    if (cfg.separator) {
      const sep = document.createElement('div');
      sep.className = 'ctx-menu-sep';
      menu.appendChild(sep);
      return;
    }

    const item = document.createElement('div');
    item.className = 'ctx-menu-item' + (cfg.danger ? ' danger' : '');
    item.setAttribute('role', 'menuitem');
    item.tabIndex = 0;

    const icon = document.createElement('i');
    icon.setAttribute('data-lucide', cfg.icon);
    const text = document.createElement('span');
    text.textContent = cfg.label;
    item.append(icon, text);

    item.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
      closeContextMenu();
      cfg.onClick();
    });
    menu.appendChild(item);
  });

  anchorEl.style.position = 'relative';
  anchorEl.appendChild(menu);
  activeContextMenu = menu;
  refreshIcons();
}

/**
 * 注册全局事件：点击空白处或按 Escape 关闭右键菜单。
 */
export function initDialogGlobalHandlers() {
  document.addEventListener('click', (e) => {
    if (activeContextMenu
      && !activeContextMenu.contains(e.target)
      && !e.target.closest('.history-item-more')) {
      closeContextMenu();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeContextMenu();
  });
}
