/**
 * settings.js —— 设置面板（双栏主从布局）
 *
 * 后端 /api/settings 返回 { categories, items } 两段结构：
 *   categories: [{ id, label, icon, description }]
 *   items:      [{ key, label, type, value, default, category, description,
 *                  options, min, max, step, unit, secret, is_set,
 *                  advanced, requires_restart, is_default }]
 *
 * 布局：左侧分类导航，右侧只渲染当前选中分类的内容。
 * 相比原来把 46 项全部堆在一列，切换成本更低也更好找。
 *
 * 全部用 createElement 构建，不拼 innerHTML，避免 label/description
 * 等来自接口的字段被当作 HTML 解析。
 */

import { $, refreshIcons } from './dom-utils.js';
import * as api from './api.js';

/** 内置的两个虚拟分类，不来自后端 items */
const VIRTUAL_CATEGORIES = [
  { id: '__storage__', label: '存储', icon: 'hard-drive', description: '知识库统计与缓存清理' },
  { id: '__about__', label: '关于', icon: 'info', description: '版本与项目信息' },
];

/**
 * 规范化 select 的选项。
 * 后端可能给纯字符串数组，也可能给 [{ value, label }]。
 * @param {Array} options - 原始选项
 * @returns {Array<{value: string, label: string}>}
 */
function normalizeOptions(options) {
  if (!Array.isArray(options)) return [];
  return options.map((opt) => (
    typeof opt === 'object' && opt !== null
      ? { value: String(opt.value), label: String(opt.label ?? opt.value) }
      : { value: String(opt), label: String(opt) }
  ));
}

/**
 * 根据设置项类型创建对应的输入控件。
 * @param {Object} item - 设置项定义
 * @param {string} controlId - 控件的 id
 * @param {Function} onChange - 值变化回调，签名 (key, value) => void
 * @param {Array} modelOptions - 可用模型列表，用于补全 model 项的候选值
 * @returns {HTMLElement}
 */
function createControl(item, controlId, onChange, modelOptions) {
  const commit = (value) => onChange(item.key, value);
  let input;

  switch (item.type) {
    case 'select': {
      input = document.createElement('select');
      input.className = 'settings-select';
      let opts = normalizeOptions(item.options);
      // 后端对 model 项返回 options: null（候选列表来自 /api/models），
      // 直接渲染会得到一个空下拉框，这里用已加载的模型列表补上。
      if (!opts.length && item.key === 'model' && modelOptions.length) {
        opts = modelOptions.map((m) => ({ value: m.id, label: m.name }));
      }
      if (!opts.length) {
        opts = [{ value: String(item.value ?? ''), label: String(item.value ?? '（未配置）') }];
      }
      opts.forEach((opt) => {
        const option = document.createElement('option');
        option.value = opt.value;
        option.textContent = opt.label;
        if (opt.value === String(item.value)) option.selected = true;
        input.appendChild(option);
      });
      input.addEventListener('change', () => commit(input.value));
      break;
    }

    case 'boolean': {
      // 用 label 包一个隐藏 checkbox 做开关，比原生方框更符合现代观感
      const wrap = document.createElement('label');
      wrap.className = 'settings-switch';

      input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = item.value === true || item.value === 'true';
      input.addEventListener('change', () => commit(input.checked));

      const track = document.createElement('span');
      track.className = 'settings-switch-track';

      wrap.append(input, track);
      input.id = controlId;
      input.name = item.key;
      wrap.htmlFor = controlId;
      return wrap;
    }

    case 'int':
    case 'number': {
      input = document.createElement('input');
      input.type = 'number';
      input.className = 'settings-input settings-input-number';
      input.value = item.value ?? '';
      if (item.min !== null && item.min !== undefined) input.min = item.min;
      if (item.max !== null && item.max !== undefined) input.max = item.max;
      // 整数类型强制步长为 1，避免出现小数
      input.step = item.type === 'int' ? 1 : (item.step ?? 'any');
      input.addEventListener('change', () => {
        const num = item.type === 'int'
          ? parseInt(input.value, 10)
          : parseFloat(input.value);
        commit(Number.isNaN(num) ? item.default : num);
      });
      break;
    }

    case 'textarea': {
      input = document.createElement('textarea');
      input.className = 'settings-textarea';
      input.rows = 3;
      input.value = item.value ?? '';
      input.addEventListener('change', () => commit(input.value));
      break;
    }

    case 'password': {
      input = document.createElement('input');
      input.type = 'password';
      input.className = 'settings-input';
      // 密钥类字段后端返回的是掩码，占位提示用户留空即不修改
      input.value = '';
      input.placeholder = item.is_set ? (item.value || '已配置，留空则不修改') : '未配置';
      input.addEventListener('change', () => {
        if (input.value.trim()) commit(input.value.trim());
      });
      break;
    }

    default: {
      input = document.createElement('input');
      input.type = 'text';
      input.className = 'settings-input';
      input.value = item.value ?? '';
      input.addEventListener('change', () => commit(input.value));
    }
  }

  input.id = controlId;
  input.name = item.key;
  return input;
}

/**
 * 创建一个完整的设置项行。
 * @param {Object} item - 设置项定义
 * @param {Function} onChange - 值变化回调
 * @param {Array} modelOptions - 可用模型列表
 * @returns {HTMLElement}
 */
function createSettingItem(item, onChange, modelOptions) {
  const wrap = document.createElement('div');
  wrap.className = 'settings-item';
  wrap.dataset.key = item.key;

  const controlId = 'setting-' + item.key;

  const info = document.createElement('div');
  info.className = 'settings-item-info';

  const head = document.createElement('div');
  head.className = 'settings-item-head';

  const label = document.createElement('label');
  label.className = 'settings-item-label';
  label.htmlFor = controlId;
  label.textContent = item.label || item.key;
  head.appendChild(label);

  // 非默认值加个标记，方便一眼看出改过哪些
  if (item.is_default === false) {
    const badge = document.createElement('span');
    badge.className = 'settings-badge settings-badge-modified';
    badge.textContent = '已修改';
    head.appendChild(badge);
  }
  if (item.requires_restart) {
    const badge = document.createElement('span');
    badge.className = 'settings-badge settings-badge-restart';
    badge.textContent = '需重启';
    badge.title = '修改后需要重启服务才能生效';
    head.appendChild(badge);
  }
  info.appendChild(head);

  if (item.description) {
    const desc = document.createElement('p');
    desc.className = 'settings-item-desc';
    desc.textContent = item.description;
    info.appendChild(desc);
  }

  const control = document.createElement('div');
  control.className = 'settings-item-control';
  control.appendChild(createControl(item, controlId, onChange, modelOptions));

  if (item.unit) {
    const unit = document.createElement('span');
    unit.className = 'settings-item-unit';
    unit.textContent = item.unit;
    control.appendChild(unit);
  }

  wrap.append(info, control);
  return wrap;
}

/**
 * 渲染某个分类下的所有设置项。
 * @param {Array} items - 该分类的设置项
 * @param {Function} onChange - 值变化回调
 * @param {Array} modelOptions - 可用模型列表
 * @returns {DocumentFragment}
 */
function renderCategoryItems(items, onChange, modelOptions) {
  const frag = document.createDocumentFragment();

  const basic = items.filter((i) => !i.advanced);
  const advanced = items.filter((i) => i.advanced);

  const group = document.createElement('div');
  group.className = 'settings-group';
  basic.forEach((item) => group.appendChild(createSettingItem(item, onChange, modelOptions)));
  frag.appendChild(group);

  // 高级选项收进折叠区，避免不常用的参数干扰主流程
  if (advanced.length) {
    const details = document.createElement('details');
    details.className = 'settings-advanced';

    const summary = document.createElement('summary');
    summary.className = 'settings-advanced-summary';
    const arrow = document.createElement('i');
    arrow.setAttribute('data-lucide', 'chevron-right');
    const summaryText = document.createElement('span');
    summaryText.textContent = `高级选项（${advanced.length}）`;
    summary.append(arrow, summaryText);
    details.appendChild(summary);

    const advGroup = document.createElement('div');
    advGroup.className = 'settings-group';
    advanced.forEach((item) => advGroup.appendChild(createSettingItem(item, onChange, modelOptions)));
    details.appendChild(advGroup);

    frag.appendChild(details);
  }

  return frag;
}

/**
 * 渲染"存储"分类：知识库统计 + 缓存清理。
 * @param {Function} onClearCache - 清理缓存回调
 * @returns {DocumentFragment}
 */
function renderStorage(onClearCache) {
  const frag = document.createDocumentFragment();

  const statsCard = document.createElement('div');
  statsCard.className = 'settings-stats-card';
  statsCard.id = 'statsDisplay';
  statsCard.textContent = '加载统计中...';
  frag.appendChild(statsCard);

  const actions = document.createElement('div');
  actions.className = 'settings-actions-row';
  [['embedding', '清除嵌入缓存'], ['answer', '清除回答缓存']].forEach(([type, text]) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'settings-action-btn';
    btn.textContent = text;
    btn.addEventListener('click', () => onClearCache(type, btn));
    actions.appendChild(btn);
  });
  frag.appendChild(actions);

  return frag;
}

/**
 * 渲染"关于"分类。
 * @returns {DocumentFragment}
 */
function renderAbout() {
  const frag = document.createDocumentFragment();

  const about = document.createElement('div');
  about.className = 'settings-about';

  const name = document.createElement('div');
  name.className = 'settings-about-name';
  name.textContent = '马列通 MarxLen';

  const desc = document.createElement('div');
  desc.className = 'settings-about-desc';
  desc.textContent = '基于马克思主义经典著作的智能问答系统';

  const version = document.createElement('div');
  version.className = 'settings-about-version';
  version.textContent = 'v1.1.0';

  about.append(name, desc, version);
  frag.appendChild(about);
  return frag;
}

/**
 * 拉取并展示知识库统计。
 */
async function loadStats() {
  const el = $('#statsDisplay');
  if (!el) return;
  try {
    const s = await api.getStats();
    el.textContent = '';
    const rows = [
      ['文档', s.document_count],
      ['向量', s.vector_count],
      ['源文件', s.source_files],
      ['嵌入缓存', s.cache_embeddings],
      ['回答缓存', s.cache_answers],
    ];
    rows.forEach(([label, value]) => {
      const row = document.createElement('div');
      row.className = 'settings-stat-row';
      const k = document.createElement('span');
      k.className = 'settings-stat-label';
      k.textContent = label;
      const v = document.createElement('span');
      v.className = 'settings-stat-value';
      v.textContent = Number(value).toLocaleString('zh-CN');
      row.append(k, v);
      el.appendChild(row);
    });
  } catch (err) {
    el.textContent = '统计加载失败: ' + err.message;
  }
}

/**
 * 加载并渲染整个设置面板。
 * @param {Object} refs - DOM 引用
 * @param {HTMLElement} refs.nav - 左侧分类导航容器
 * @param {HTMLElement} refs.body - 右侧内容容器
 * @param {HTMLElement} refs.title - 右侧标题
 * @param {HTMLElement} refs.desc - 右侧描述
 * @param {Function} onModelChange - 模型项变化时的回调，参数为模型 ID
 * @param {Array} [modelOptions=[]] - 可用模型列表，用于补全 model 项的下拉候选
 */
export async function loadSettings(refs, onModelChange, modelOptions = []) {
  const { nav, body, title, desc } = refs;

  body.textContent = '';
  const loading = document.createElement('p');
  loading.className = 'settings-loading';
  loading.textContent = '加载中...';
  body.appendChild(loading);

  let data;
  try {
    data = await api.getSettings();
  } catch (err) {
    body.textContent = '';
    const error = document.createElement('p');
    error.className = 'settings-error';
    error.textContent = '加载设置失败: ' + err.message;
    body.appendChild(error);
    return;
  }

  const items = data.items || [];

  /**
   * 提交单个设置项的修改，并给出即时反馈。
   * @param {string} key - 设置键
   * @param {*} value - 新值
   */
  const handleChange = async (key, value) => {
    const row = body.querySelector(`.settings-item[data-key="${key}"]`);
    try {
      await api.updateSettings({ [key]: value });
      if (row) {
        row.classList.add('saved');
        setTimeout(() => row.classList.remove('saved'), 1200);
      }
      if (key === 'model' && value) onModelChange(value);
    } catch (err) {
      console.error('更新设置失败:', err);
      if (row) {
        row.classList.add('save-failed');
        setTimeout(() => row.classList.remove('save-failed'), 2000);
      }
    }
  };

  /**
   * 清理缓存并刷新统计。
   * @param {string} type - 缓存类型
   * @param {HTMLElement} btn - 触发按钮
   */
  const handleClearCache = async (type, btn) => {
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '清理中...';
    try {
      await api.clearCache(type);
      await loadStats();
      btn.textContent = '已清除';
    } catch (err) {
      console.error('清除缓存失败:', err);
      btn.textContent = '清除失败';
    }
    setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
  };

  // 按 category 归类
  const grouped = {};
  items.forEach((item) => {
    const cat = item.category || 'general';
    (grouped[cat] = grouped[cat] || []).push(item);
  });

  // 后端声明的分类里，只保留真的有配置项的
  const realCategories = (data.categories || []).filter(
    (c) => grouped[c.id] && grouped[c.id].length,
  );
  // 兜底：items 里出现但 categories 没声明的
  const declaredIds = new Set(realCategories.map((c) => c.id));
  Object.keys(grouped).forEach((catId) => {
    if (!declaredIds.has(catId)) {
      realCategories.push({ id: catId, label: catId, icon: 'sliders-horizontal', description: '' });
    }
  });

  const allCategories = [...realCategories, ...VIRTUAL_CATEGORIES];

  /**
   * 切换到指定分类。
   * @param {Object} category - 分类定义
   */
  const selectCategory = (category) => {
    // 同步导航项的选中态
    nav.querySelectorAll('.settings-nav-item').forEach((el) => {
      const active = el.dataset.category === category.id;
      el.classList.toggle('active', active);
      el.setAttribute('aria-selected', active ? 'true' : 'false');
      el.tabIndex = active ? 0 : -1;
    });

    title.textContent = category.label;
    desc.textContent = category.description || '';
    desc.style.display = category.description ? '' : 'none';

    body.textContent = '';
    body.scrollTop = 0;

    if (category.id === '__storage__') {
      body.appendChild(renderStorage(handleClearCache));
      loadStats();
    } else if (category.id === '__about__') {
      body.appendChild(renderAbout());
    } else {
      body.appendChild(renderCategoryItems(grouped[category.id], handleChange, modelOptions));
    }
    refreshIcons();
  };

  // ── 渲染左侧导航 ──
  nav.textContent = '';
  allCategories.forEach((category, index) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.className = 'settings-nav-item';
    item.dataset.category = category.id;
    item.setAttribute('role', 'tab');
    item.setAttribute('aria-selected', 'false');
    item.tabIndex = -1;

    const icon = document.createElement('i');
    icon.setAttribute('data-lucide', category.icon || 'settings');
    const text = document.createElement('span');
    text.textContent = category.label;
    item.append(icon, text);

    item.addEventListener('click', () => selectCategory(category));

    // 上下键在分类间移动，符合 tablist 的键盘惯例
    item.addEventListener('keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      e.preventDefault();
      const step = e.key === 'ArrowDown' ? 1 : -1;
      const next = allCategories[(index + step + allCategories.length) % allCategories.length];
      selectCategory(next);
      nav.querySelector(`[data-category="${next.id}"]`).focus();
    });

    nav.appendChild(item);
  });

  refreshIcons();
  if (allCategories.length) selectCategory(allCategories[0]);
}
