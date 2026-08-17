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
 *
 * 支持分类内小节:配置项带 section 时,按 section 分组渲染小节标题
 * (如"系统"分类下的 对话/缓存/服务),不带 section 的项归入"常规"。
 * 每节内仍按 基础/高级 两层组织,高级项收进折叠区。
 *
 * @param {Array} items - 该分类的设置项
 * @param {Function} onChange - 值变化回调
 * @param {Array} modelOptions - 可用模型列表
 * @returns {DocumentFragment}
 */
function renderCategoryItems(items, onChange, modelOptions) {
  const frag = document.createDocumentFragment();

  // 按 section 分组:undefined/空归入默认小节,保持声明顺序
  const sections = new Map();
  const defaults = [];
  items.forEach((item) => {
    if (item.section) {
      if (!sections.has(item.section)) sections.set(item.section, []);
      sections.get(item.section).push(item);
    } else {
      defaults.push(item);
    }
  });

  const renderGroup = (list) => {
    const basic = list.filter((i) => !i.advanced);
    const advanced = list.filter((i) => i.advanced);

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
  };

  // 无小节项在前,然后按声明顺序渲染各小节
  if (defaults.length) renderGroup(defaults);
  sections.forEach((list, name) => {
    const heading = document.createElement('div');
    heading.className = 'settings-section-title';
    heading.textContent = name;
    frag.appendChild(heading);
    renderGroup(list);
  });

  return frag;
}

// ======================================================================
// "接口"分类定制:服务商预设 + API 连通性测试
// ======================================================================

/**
 * 服务商预设。任何 OpenAI 兼容服务都可用于对话模型;
 * 嵌入/重排需要服务商额外提供对应模型。
 */
const PROVIDERS = [
  {
    id: 'deepseek', name: 'DeepSeek',
    base: 'https://api.deepseek.com/v1',
    chat: true, embed: false, rerank: false, thinking: true,
    note: '对话模型最全且支持思考强度;无嵌入/重排服务,嵌入与重排需另配(如硅基流动)',
  },
  {
    id: 'siliconflow', name: '硅基流动 SiliconFlow',
    base: 'https://api.siliconflow.cn/v1',
    chat: true, embed: true, rerank: true, thinking: false,
    note: '对话 + 嵌入 + 重排一家配齐;嵌入模型用 Qwen3-Embedding-0.6B',
  },
  {
    id: 'openai', name: 'OpenAI',
    base: 'https://api.openai.com/v1',
    chat: true, embed: true, rerank: false, thinking: false,
    note: '标准 OpenAI;无 rerank 服务,思考强度取决于所用模型是否支持',
  },
  {
    id: 'qwen', name: '通义千问(阿里云百炼)',
    base: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    chat: true, embed: true, rerank: true, thinking: false,
    note: '需用兼容模式地址;嵌入用 text-embedding 系,重排用 gte-rerank',
  },
  {
    id: 'kimi', name: 'Kimi(Moonshot)',
    base: 'https://api.moonshot.cn/v1',
    chat: true, embed: false, rerank: false, thinking: false,
    note: '仅对话;无嵌入/重排服务',
  },
  {
    id: 'zhipu', name: '智谱 GLM',
    base: 'https://open.bigmodel.cn/api/paas/v4',
    chat: true, embed: true, rerank: false, thinking: false,
    note: '对话 + 嵌入;无 rerank 服务',
  },
  {
    id: 'vllm', name: '本地 vLLM / Ollama',
    base: 'http://localhost:8000/v1',
    chat: true, embed: true, rerank: false, thinking: false,
    note: '自托管;Ollama 请使用其 /v1 兼容端点,嵌入模型需自行部署',
  },
];

/** 服务商预设卡:选服务商 → 一键填入兼容地址 */
function renderProviderCard(onChange) {
  const card = document.createElement('div');
  card.className = 'settings-provider-card';

  const head = document.createElement('div');
  head.className = 'settings-provider-head';
  const title = document.createElement('span');
  title.className = 'settings-group-title';
  title.textContent = '服务商预设';
  const hint = document.createElement('span');
  hint.className = 'settings-provider-hint';
  hint.textContent = '一键填入兼容地址,填入后请补填各自的 API Key';
  head.append(title, hint);
  card.appendChild(head);

  const row = document.createElement('div');
  row.className = 'settings-provider-row';

  const select = document.createElement('select');
  select.className = 'settings-select';
  PROVIDERS.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.name;
    select.appendChild(opt);
  });
  row.appendChild(select);

  const apply = document.createElement('button');
  apply.type = 'button';
  apply.className = 'settings-action-btn';
  apply.textContent = '应用预设';
  row.appendChild(apply);
  card.appendChild(row);

  const info = document.createElement('div');
  info.className = 'settings-provider-info';

  const badges = document.createElement('div');
  badges.className = 'settings-provider-badges';

  const renderInfo = () => {
    const p = PROVIDERS.find((x) => x.id === select.value) || PROVIDERS[0];
    badges.textContent = '';
    [['chat', '对话'], ['embed', '嵌入'], ['rerank', '重排'],
     ['thinking', '思考强度']].forEach(([field, label]) => {
      const b = document.createElement('span');
      b.className = 'settings-provider-badge '
        + (p[field] ? 'yes' : 'no');
      b.textContent = `${label}${p[field] ? ' ✓' : ' ✗'}`;
      badges.appendChild(b);
    });
    note.textContent = p.note || '';
  };
  const note = document.createElement('div');
  note.className = 'settings-provider-note';
  info.append(badges, note);
  card.appendChild(info);

  select.addEventListener('change', renderInfo);

  apply.addEventListener('click', () => {
    const p = PROVIDERS.find((x) => x.id === select.value) || PROVIDERS[0];
    // 填入对话 API 地址;提供嵌入服务时同时填嵌入地址
    const chatInput = document.getElementById('setting-api_base_url');
    if (chatInput) {
      chatInput.value = p.base;
      onChange('api_base_url', p.base);
    }
    if (p.embed) {
      const embedInput = document.getElementById('setting-embed_api_base_url');
      if (embedInput) {
        embedInput.value = p.base;
        onChange('embed_api_base_url', p.base);
      }
    }
    apply.textContent = '已应用';
    setTimeout(() => { apply.textContent = '应用预设'; }, 1500);
  });

  renderInfo();
  return card;
}

/** 读取输入框当前值(空则返回 '',由后端回退到已保存配置) */
function readControlValue(key) {
  const el = document.getElementById('setting-' + key);
  return el && el.value ? el.value.trim() : '';
}

/** 测试按钮 + 结果行 */
function createTestRow(label, onTest) {
  const wrap = document.createElement('div');
  wrap.className = 'settings-test-row';

  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'settings-test-btn';
  btn.textContent = label;

  const result = document.createElement('span');
  result.className = 'settings-test-result';

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    result.className = 'settings-test-result';
    result.textContent = '测试中...';
    try {
      const r = await onTest();
      result.className = 'settings-test-result ' + (r.ok ? 'ok' : 'fail');
      result.textContent = r.ok
        ? `✓ ${r.latency_ms}ms · ${r.detail}`
        : `✗ ${r.detail}`;
    } catch (err) {
      result.className = 'settings-test-result fail';
      result.textContent = '✗ ' + err.message;
    }
    btn.disabled = false;
  });

  wrap.append(btn, result);
  return wrap;
}

/** 组标题 + 内容块 */
function renderGroup(titleText, children) {
  const box = document.createElement('div');
  box.className = 'settings-group';
  const title = document.createElement('div');
  title.className = 'settings-group-title';
  title.textContent = titleText;
  box.appendChild(title);
  children.forEach((c) => box.appendChild(c));
  return box;
}

/**
 * 模型管理卡:直接在 GUI 添加/移除模型,无需手编 .env 的
 * OPENAI_MODEL_LIST。添加后立即出现在模型选择器中,无需重启。
 *
 * @param {Array} modelOptions - 当前可用模型列表
 * @param {Function} onModelsChanged - 增删后的刷新回调
 */
function renderModelManager(modelOptions, onModelsChanged) {
  const card = document.createElement('div');
  card.className = 'settings-provider-card';

  const head = document.createElement('div');
  head.className = 'settings-provider-head';
  const title = document.createElement('span');
  title.className = 'settings-group-title';
  title.textContent = '模型管理';
  const hint = document.createElement('span');
  hint.className = 'settings-provider-hint';
  hint.textContent = '添加后即可在输入框的模型选择器中切换,无需重启';
  head.append(title, hint);
  card.appendChild(head);

  // ── 添加行 ──
  const row = document.createElement('div');
  row.className = 'settings-model-add-row';

  const idInput = document.createElement('input');
  idInput.type = 'text';
  idInput.className = 'settings-input';
  idInput.placeholder = '模型 ID，如 deepseek-v4-flash';
  idInput.setAttribute('aria-label', '模型 ID');

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'settings-input';
  nameInput.placeholder = '显示名（可空）';
  nameInput.setAttribute('aria-label', '显示名');

  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'settings-action-btn';
  addBtn.textContent = '添加';

  const status = document.createElement('span');
  status.className = 'settings-test-result';
  status.style.marginTop = '0';

  row.append(idInput, nameInput, addBtn);
  card.appendChild(row);
  card.appendChild(status);

  const doAdd = async () => {
    const id = idInput.value.trim();
    if (!id) {
      status.className = 'settings-test-result fail';
      status.textContent = '请输入模型 ID';
      return;
    }
    addBtn.disabled = true;
    status.className = 'settings-test-result';
    status.textContent = '添加中...';
    try {
      const r = await api.addModel(id, nameInput.value.trim());
      status.className = 'settings-test-result ok';
      status.textContent = r.added ? `已添加 ${id}` : `已更新 ${id} 的显示名`;
      idInput.value = '';
      nameInput.value = '';
      if (onModelsChanged) onModelsChanged();
    } catch (err) {
      status.className = 'settings-test-result fail';
      status.textContent = '添加失败: ' + err.message;
    }
    addBtn.disabled = false;
  };
  addBtn.addEventListener('click', doAdd);
  idInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doAdd(); });

  // ── 已配置模型列表 ──
  const list = document.createElement('div');
  list.className = 'settings-model-list';
  (modelOptions || []).forEach((m) => {
    const item = document.createElement('div');
    item.className = 'settings-model-item';

    const info = document.createElement('div');
    info.className = 'settings-model-item-info';
    const name = document.createElement('span');
    name.className = 'settings-model-item-name';
    name.textContent = m.name;
    const id = document.createElement('span');
    id.className = 'settings-model-item-id';
    id.textContent = m.id;
    info.append(name, id);

    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'settings-model-remove';
    rm.title = '移除该模型';
    rm.setAttribute('aria-label', `移除 ${m.name}`);
    rm.textContent = '✕';
    rm.addEventListener('click', async () => {
      rm.disabled = true;
      try {
        await api.removeModel(m.id);
        if (onModelsChanged) onModelsChanged();
      } catch (err) {
        status.className = 'settings-test-result fail';
        status.textContent = '移除失败: ' + err.message;
        rm.disabled = false;
      }
    });
    item.append(info, rm);
    list.appendChild(item);
  });
  if (!(modelOptions || []).length) {
    const empty = document.createElement('div');
    empty.className = 'settings-model-item-id';
    empty.textContent = '（暂无其他模型，可在上方添加）';
    list.appendChild(empty);
  }
  card.appendChild(list);

  return card;
}

/**
 * "接口"分类的定制渲染:预设卡 + 分组 + 测试按钮。
 */
function renderApiCategory(items, onChange, modelOptions) {
  const byKey = {};
  items.forEach((i) => { byKey[i.key] = i; });
  const frag = document.createDocumentFragment();

  frag.appendChild(renderProviderCard(onChange));

  // ── 对话模型 API ──
  const chatGroup = ['api_base_url', 'api_key', 'model']
    .filter((k) => byKey[k])
    .map((k) => createSettingItem(byKey[k], onChange, modelOptions));
  chatGroup.push(createTestRow('测试对话 API', () => api.testApi({
    target: 'chat',
    api_base_url: readControlValue('api_base_url'),
    api_key: readControlValue('api_key'),
  })));
  frag.appendChild(renderGroup('对话模型 API', chatGroup));

  // ── 嵌入 / 重排 API ──
  const embedKeys = ['embed_api_base_url', 'embed_api_key',
    'embed_model', 'rerank_model'];
  const embedGroup = embedKeys
    .filter((k) => byKey[k])
    .map((k) => createSettingItem(byKey[k], onChange, modelOptions));
  embedGroup.push(createTestRow('测试嵌入', () => api.testApi({
    target: 'embed',
    api_base_url: readControlValue('embed_api_base_url'),
    api_key: readControlValue('embed_api_key'),
    model: readControlValue('embed_model'),
  })));
  embedGroup.push(createTestRow('测试重排', () => api.testApi({
    target: 'rerank',
    api_base_url: readControlValue('embed_api_base_url'),
    api_key: readControlValue('embed_api_key'),
    model: readControlValue('rerank_model'),
  })));
  frag.appendChild(renderGroup('嵌入 / 重排 API', embedGroup));

  // ── 其余项(超时、模型列表等) ──
  const GROUPED_KEYS = new Set(['api_base_url', 'api_key', 'model',
    'embed_api_base_url', 'embed_api_key',
    'embed_model', 'rerank_model']);
  const rest = items.filter((i) => !GROUPED_KEYS.has(i.key));
  if (rest.length) {
    const restFrag = renderCategoryItems(rest, onChange, modelOptions);
    frag.appendChild(restFrag);
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
      ['知识库版本', s.kb_version || 'legacy'],
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
      v.textContent = typeof value === 'number'
        ? Number(value).toLocaleString('zh-CN')
        : String(value ?? '');
      row.append(k, v);
      el.appendChild(row);
    });

    // 请求性能汇总（最近 N 次请求的平均耗时，问题 12）
    const perf = s.perf || {};
    if (perf.requests) {
      const fmt = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(1) + 's'
        : Math.round(ms) + 'ms');
      const perfParts = [];
      if (perf.avg_analyze_ms != null) perfParts.push(`解构 ${fmt(perf.avg_analyze_ms)}`);
      if (perf.avg_retrieve_ms != null) perfParts.push(`检索 ${fmt(perf.avg_retrieve_ms)}`);
      if (perf.avg_first_token_ms != null) perfParts.push(`首字 ${fmt(perf.avg_first_token_ms)}`);
      if (perf.avg_generate_ms != null) perfParts.push(`生成 ${fmt(perf.avg_generate_ms)}`);
      if (perf.avg_total_ms != null) perfParts.push(`总计 ${fmt(perf.avg_total_ms)}`);

      const perfRow = document.createElement('div');
      perfRow.className = 'settings-stat-row';
      const pk = document.createElement('span');
      pk.className = 'settings-stat-label';
      pk.textContent = `平均耗时（近 ${perf.requests} 次）`;
      const pv = document.createElement('span');
      pv.className = 'settings-stat-value';
      pv.textContent = perfParts.join(' · ') || '暂无数据';
      perfRow.append(pk, pv);
      el.appendChild(perfRow);
    }
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
 * @param {Function} [onModelsChanged] - 模型列表增删后的回调（刷新主界面下拉）
 */
export async function loadSettings(refs, onModelChange, modelOptions = [],
                                   onModelsChanged = null) {
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

  // 可变模型列表容器:模型增删后 handleModelsChanged 更新其内容,
  // 避免闭包里的原始数组引用失效导致重渲染拿到旧数据
  const modelRef = { items: modelOptions || [] };

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

  // 当前激活的分类(模型管理增删后据此重渲染)
  let activeCategory = null;

  /**
   * 切换到指定分类。
   * @param {Object} category - 分类定义
   */
  const selectCategory = (category) => {
    activeCategory = category;
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
    } else if (category.id === 'api') {
      // 接口分类定制:服务商预设 + API 测试
      body.appendChild(renderApiCategory(grouped[category.id], handleChange, modelRef.items));
    } else if (category.id === 'model') {
      // 模型分类定制:顶部模型管理卡(直接添加/移除),下方参数
      body.appendChild(renderModelManager(modelRef.items, handleModelsChanged));
      body.appendChild(renderCategoryItems(grouped[category.id], handleChange, modelRef.items));
    } else {
      body.appendChild(renderCategoryItems(grouped[category.id], handleChange, modelRef.items));
    }
    refreshIcons();
  };

  /**
   * 模型列表增删后:刷新主界面下拉,并重渲染当前分类让列表同步。
   *
   * 渲染用的是 modelRef(可变容器)而非闭包里的原始 modelOptions——
   * 主界面 loadModels 会重新赋值 state.availableModels,旧数组引用
   * 拿不到最新列表,直接重渲染会显示增删前的数据。
   */
  const handleModelsChanged = async () => {
    if (onModelsChanged) onModelsChanged();
    try {
      const latest = await api.listModels();
      // /models 现在返回 {current, models} 结构,设置页列表用 models
      modelRef.items = latest.models || [];
    } catch (e) {
      console.warn('刷新模型列表失败:', e);
    }
    if (activeCategory) selectCategory(activeCategory);
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
