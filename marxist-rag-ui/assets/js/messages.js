/**
 * messages.js —— 消息气泡、来源卡片、操作栏、对话大纲
 *
 * 负责会话区内所有 DOM 的构建与更新。
 * 用户可控的文本一律走 textContent，只有经 DOMPurify 净化过的
 * Markdown 才允许进 innerHTML。
 */

import { $, $$, refreshIcons, createIconButton, scrollToBottom } from './dom-utils.js';
import { renderMarkdown, hardenLinks } from './markdown.js';
import { TEXTAREA_MAX_HEIGHT } from './config.js';
import { jumpToSource } from './reader.js';

/** 把原始提问文本挂在 DOM 上，避免从渲染后的 HTML 反推 */
const ORIGINAL_TEXT_KEY = '_originalText';

/**
 * 记录消息行对应的原始文本。
 * @param {HTMLElement} row - 消息行元素
 * @param {string} text - 原始文本
 */
export function setRowOriginalText(row, text) {
  row[ORIGINAL_TEXT_KEY] = text;
}

/**
 * 读取消息行的原始文本。
 * @param {HTMLElement} row - 消息行元素
 * @returns {string}
 */
export function getRowOriginalText(row) {
  return row[ORIGINAL_TEXT_KEY] || '';
}

/**
 * 创建用户消息行。
 * @param {string} content - 消息文本
 * @param {Function} onEdit - 点击编辑按钮的回调，签名 (row, text) => void
 * @param {HTMLElement} [variantSwitch] - 版本切换器，改过多次提问时才有
 * @returns {HTMLElement}
 */
export function createUserMessage(content, onEdit, variantSwitch) {
  const row = document.createElement('div');
  row.className = 'message-row user-message';
  setRowOriginalText(row, content);

  const body = document.createElement('div');
  body.className = 'msg-body';

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  const text = document.createElement('div');
  text.className = 'msg-text';
  const p = document.createElement('p');
  p.textContent = content;
  text.appendChild(p);
  bubble.appendChild(text);
  body.appendChild(bubble);

  const actions = document.createElement('div');
  actions.className = 'user-msg-actions';
  if (variantSwitch) actions.appendChild(variantSwitch);
  const editBtn = createIconButton('pencil', '编辑', 'msg-action-btn');
  editBtn.addEventListener('click', () => onEdit(row, getRowOriginalText(row)));
  actions.appendChild(editBtn);
  body.appendChild(actions);

  row.appendChild(body);
  refreshIcons();
  return row;
}

/**
 * 把处于编辑态的用户消息还原为普通显示态。
 * @param {HTMLElement} row - 消息行
 * @param {string} text - 要显示的文本
 * @param {Function} onEdit - 编辑按钮回调
 */
export function restoreUserMessage(row, text, onEdit) {
  row.classList.remove('editing');
  setRowOriginalText(row, text);

  const body = $('.msg-body', row);
  body.innerHTML = '';

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  const textDiv = document.createElement('div');
  textDiv.className = 'msg-text';
  const p = document.createElement('p');
  p.textContent = text;
  textDiv.appendChild(p);
  bubble.appendChild(textDiv);
  body.appendChild(bubble);

  const actions = document.createElement('div');
  actions.className = 'user-msg-actions';
  const editBtn = createIconButton('pencil', '编辑', 'msg-action-btn');
  editBtn.addEventListener('click', () => onEdit(row, text));
  actions.appendChild(editBtn);
  body.appendChild(actions);

  refreshIcons();
}

/**
 * 把用户消息切换为就地编辑态。
 * @param {HTMLElement} row - 消息行
 * @param {Object} handlers - 回调集合
 * @param {Function} handlers.onSubmit - 提交新文本，签名 (newText) => void
 * @param {Function} handlers.onCancel - 取消编辑，签名 (text) => void
 */
export function enterEditMode(row, handlers) {
  const currentText = getRowOriginalText(row);
  row.classList.add('editing');

  const bubble = $('.msg-bubble', row);
  bubble.innerHTML = '';

  const textDiv = document.createElement('div');
  textDiv.className = 'msg-text';

  const editArea = document.createElement('textarea');
  editArea.className = 'msg-edit-area';
  editArea.placeholder = '编辑消息...';
  editArea.value = currentText;
  editArea.setAttribute('aria-label', '编辑消息内容');

  const actions = document.createElement('div');
  actions.className = 'msg-edit-actions';

  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.className = 'msg-edit-btn secondary';
  cancelBtn.textContent = '取消';

  const submitBtn = document.createElement('button');
  submitBtn.type = 'button';
  submitBtn.className = 'msg-edit-btn primary';
  submitBtn.textContent = '发送';

  actions.append(cancelBtn, submitBtn);
  textDiv.append(editArea, actions);
  bubble.appendChild(textDiv);

  // 自适应高度
  const resize = () => {
    editArea.style.height = 'auto';
    editArea.style.height = Math.min(editArea.scrollHeight, TEXTAREA_MAX_HEIGHT) + 'px';
  };
  editArea.addEventListener('input', resize);
  resize();

  editArea.focus();
  editArea.setSelectionRange(currentText.length, currentText.length);

  cancelBtn.addEventListener('click', () => handlers.onCancel(currentText));
  submitBtn.addEventListener('click', () => {
    const newText = editArea.value.trim();
    if (newText) handlers.onSubmit(newText);
  });

  editArea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitBtn.click(); }
    if (e.key === 'Escape') { e.preventDefault(); cancelBtn.click(); }
  });
}

/**
 * 创建空的助手消息容器（等待流式填充）。
 * @returns {HTMLElement}
 */
export function createAssistantMessage() {
  const row = document.createElement('div');
  row.className = 'message-row assistant-message';

  const body = document.createElement('div');
  body.className = 'msg-body';

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';

  const text = document.createElement('div');
  text.className = 'msg-text';
  // 让屏幕阅读器能感知流式增量内容
  text.setAttribute('aria-live', 'polite');
  text.setAttribute('aria-atomic', 'false');

  bubble.appendChild(text);
  body.appendChild(bubble);
  row.appendChild(body);
  return row;
}

/**
 * 创建思考过程折叠面板。
 * @param {string} [initialContent=''] - 已有的思考文本（用于恢复历史）
 * @param {boolean} [done=false] - 是否已完成思考
 * @returns {HTMLElement}
 */
export function createThinkingPanel(initialContent = '', done = false) {
  const panel = document.createElement('div');
  panel.className = 'thinking-process' + (initialContent ? ' visible' : '');

  const header = document.createElement('div');
  header.className = 'thinking-process-header';
  header.tabIndex = 0;
  header.setAttribute('role', 'button');
  header.setAttribute('aria-expanded', 'false');

  const icon = document.createElement('i');
  icon.setAttribute('data-lucide', 'brain');
  icon.className = 'thinking-process-icon';
  if (done) icon.style.animation = 'none';

  const label = document.createElement('span');
  label.className = 'thinking-process-label';
  label.textContent = done ? '已深度思考' : '正在深度思考...';

  const duration = document.createElement('span');
  duration.className = 'thinking-process-duration';

  const arrow = document.createElement('i');
  arrow.setAttribute('data-lucide', 'chevron-down');
  arrow.className = 'thinking-process-arrow';

  header.append(icon, label, duration, arrow);

  const bodyEl = document.createElement('div');
  bodyEl.className = 'thinking-process-body';
  bodyEl.textContent = initialContent;

  panel.append(header, bodyEl);

  const toggle = () => {
    const open = panel.classList.toggle('open');
    header.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  header.addEventListener('click', toggle);
  header.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });

  return panel;
}

/**
 * 创建联网搜索参考面板。
 * @returns {HTMLElement}
 */
export function createSearchRefsPanel() {
  const panel = document.createElement('div');
  panel.className = 'search-references';

  const header = document.createElement('div');
  header.className = 'search-ref-header';
  header.tabIndex = 0;
  header.setAttribute('role', 'button');
  header.setAttribute('aria-expanded', 'false');

  const globe = document.createElement('i');
  globe.setAttribute('data-lucide', 'globe');
  const label = document.createElement('span');
  label.textContent = '已搜索相关资料';
  const arrow = document.createElement('i');
  arrow.setAttribute('data-lucide', 'chevron-down');
  arrow.className = 'search-ref-arrow';

  header.append(globe, label, arrow);

  const list = document.createElement('div');
  list.className = 'search-ref-list';

  panel.append(header, list);

  const toggle = () => {
    const open = panel.classList.toggle('open');
    header.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  header.addEventListener('click', toggle);
  header.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });

  return panel;
}

/**
 * 往参考面板里填充搜索结果链接。
 * @param {HTMLElement} panel - createSearchRefsPanel 返回的面板
 * @param {Array<{title: string, url: string}>} references - 参考资料
 */
export function fillSearchRefs(panel, references) {
  if (!references || !references.length) return;
  panel.classList.add('visible');
  const list = $('.search-ref-list', panel);

  references.forEach((ref, idx) => {
    const item = document.createElement('div');
    item.className = 'search-ref-item';

    const badge = document.createElement('span');
    badge.className = 'search-ref-idx';
    badge.textContent = String(idx + 1);

    const link = document.createElement('a');
    link.href = ref.url || '#';
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = ref.title || '参考来源';

    item.append(badge, link);
    list.appendChild(item);
  });
  refreshIcons();
}

/**
 * 创建等待指示器。
 *
 * 后端会通过 SSE 的 stage 事件逐阶段汇报进度（解构问题 → 检索文献 →
 * 组织回答），首个反馈 0.04s 就到，而第一个正文 token 要等 20 秒。
 * 这里按阶段追加行，让这 20 秒有内容可看。
 *
 * @returns {HTMLElement}
 */
export function createLoadingIndicator() {
  const el = document.createElement('div');
  el.className = 'thinking-steps';

  // 还没收到任何 stage 事件时的兜底行。收到第一个 stage 就会被替换掉，
  // 后端要是不支持 stage 事件，它就一直显示"正在处理..."，行为同以前
  const step = document.createElement('div');
  step.className = 'thinking-step active';
  step.dataset.stage = '_init';
  step.innerHTML = '<svg class="thinking-step-icon" width="16" height="16" viewBox="0 0 24 24" '
    + 'fill="none" stroke="currentColor" stroke-width="2">'
    + '<path d="M21 12a9 9 0 1 1-6.219-8.56"/></svg>';

  const text = document.createElement('span');
  text.className = 'step-text';
  text.textContent = '正在处理...';
  step.appendChild(text);

  el.appendChild(step);
  return el;
}

/** 阶段图标：done 用对勾，failed/skipped 用中性符号，running 用转圈 */
const STAGE_ICONS = {
  running: '<path d="M21 12a9 9 0 1 1-6.219-8.56"/>',
  done: '<path d="M20 6 9 17l-5-5"/>',
  skipped: '<path d="M5 12h14"/>',
  failed: '<path d="M12 9v4"/><path d="M12 17h.01"/>'
    + '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"/>',
};

/**
 * 按 stage 事件更新等待指示器。
 *
 * 同一个 stage 会先后收到 running 和 done，用 data-stage 找到原来那行
 * 就地更新，而不是不断追加——否则"正在检索"和"检索完成"会并排出现两行。
 *
 * @param {HTMLElement} el - createLoadingIndicator 返回的容器
 * @param {Object} data - stage 事件负载，含 stage / status / text / detail
 */
export function updateLoadingStage(el, data) {
  if (!el || !data || !data.stage) return;

  // 第一个真实阶段到达，兜底行让位
  const init = $('[data-stage="_init"]', el);
  if (init) init.remove();

  let row = $(`[data-stage="${data.stage}"]`, el);
  if (!row) {
    row = document.createElement('div');
    row.className = 'thinking-step';
    row.dataset.stage = data.stage;
    row.innerHTML = '<svg class="thinking-step-icon" width="16" height="16" viewBox="0 0 24 24" '
      + 'fill="none" stroke="currentColor" stroke-width="2"></svg>'
      + '<span class="step-text"></span>';
    el.appendChild(row);
  }

  const running = data.status === 'running';
  row.className = 'thinking-step ' + (running ? 'active' : 'done');
  if (data.status === 'failed') row.classList.add('failed');

  const icon = $('.thinking-step-icon', row);
  if (icon) icon.innerHTML = STAGE_ICONS[data.status] || STAGE_ICONS.running;
  const text = $('.step-text', row);
  if (text) text.textContent = data.text || data.stage;

  // 解构结果做成可展开的卡片，让等待期间有内容可读
  if (data.detail) appendStageDetail(row, data.detail);
}

/**
 * 在 analyze 阶段行下面挂一张可展开的解构结果卡片。
 * @param {HTMLElement} row - 阶段行
 * @param {Object} detail - 解构结果
 */
function appendStageDetail(row, detail) {
  if ($('.stage-detail', row.parentNode)) return;

  const box = document.createElement('details');
  box.className = 'stage-detail';

  const summary = document.createElement('summary');
  summary.textContent = '查看问题解构';
  box.appendChild(summary);

  const body = document.createElement('div');
  body.className = 'stage-detail-body';

  // 领域/层面/性质三个短字段并排，核心矛盾单独一行
  const tags = [detail.domain, detail.level, detail.nature].filter(Boolean);
  if (tags.length) {
    const tagRow = document.createElement('div');
    tagRow.className = 'stage-detail-tags';
    tags.forEach((t) => {
      const tag = document.createElement('span');
      tag.className = 'stage-tag';
      tag.textContent = t;
      tagRow.appendChild(tag);
    });
    body.appendChild(tagRow);
  }

  if (detail.core_contradiction) {
    body.appendChild(makeDetailLine('核心矛盾', detail.core_contradiction));
  }
  if (detail.propositions && detail.propositions.length) {
    body.appendChild(makeDetailLine('检索命题', detail.propositions.join('；')));
  }
  if (detail.keywords && detail.keywords.length) {
    body.appendChild(makeDetailLine('关键词', detail.keywords.join('、')));
  }

  box.appendChild(body);
  // 挂在阶段行之后，缩进对齐
  row.insertAdjacentElement('afterend', box);
}

/**
 * 构造"标签：内容"一行。
 * @param {string} label - 标签
 * @param {string} value - 内容
 * @returns {HTMLElement}
 */
function makeDetailLine(label, value) {
  const line = document.createElement('div');
  line.className = 'stage-detail-line';
  const k = document.createElement('span');
  k.className = 'stage-detail-key';
  k.textContent = label;
  const v = document.createElement('span');
  v.textContent = value;
  line.append(k, v);
  return line;
}

/**
 * 正文开始输出后收起进度行。
 *
 * 进度行的使命到此结束，但 analyze 阶段的解构卡片要留着——
 * 它说明了系统怎么理解这个问题，读完回答再回看仍有价值。
 * 卡片留下时整个容器不能移除，所以这里只删进度行。
 *
 * @param {HTMLElement} el - createLoadingIndicator 返回的容器
 */
export function collapseLoadingStages(el) {
  if (!el) return;
  $$('.thinking-step', el).forEach((row) => row.remove());
  // 没有解构卡片就整块拿掉，别留一个空盒子占着行高
  if (!$('.stage-detail', el) && el.parentNode) el.remove();
}

/**
 * 渲染来源卡片区。
 * @param {HTMLElement} row - 助手消息行
 * @param {Array<Object>} sources - 来源数组
 */
export function appendSources(row, sources) {
  if (!sources || !sources.length) return;
  const body = $('.msg-body', row);

  const existing = $('.sources-section', body);
  if (existing) existing.remove();

  const section = document.createElement('div');
  section.className = 'sources-section';

  const header = document.createElement('div');
  header.className = 'sources-header expanded';
  header.tabIndex = 0;
  header.setAttribute('role', 'button');
  header.setAttribute('aria-expanded', 'true');

  const headerLabel = document.createElement('span');
  headerLabel.className = 'sources-header-label';
  headerLabel.textContent = `参考来源 (${sources.length})`;
  const headerArrow = document.createElement('i');
  headerArrow.setAttribute('data-lucide', 'chevron-down');
  header.append(headerLabel, headerArrow);

  const list = document.createElement('div');
  list.className = 'sources-list visible';

  sources.forEach((s) => list.appendChild(createSourceCard(s)));

  section.append(header, list);
  body.appendChild(section);

  const toggle = () => {
    const visible = list.classList.toggle('visible');
    header.classList.toggle('expanded', visible);
    header.setAttribute('aria-expanded', visible ? 'true' : 'false');
  };
  header.addEventListener('click', toggle);
  header.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });

  refreshIcons();
}

/**
 * 创建单张来源卡片。
 * @param {Object} source - 来源数据
 * @returns {HTMLElement}
 */
function createSourceCard(source) {
  const card = document.createElement('div');
  card.className = 'source-card';

  const header = document.createElement('div');
  header.className = 'source-card-header';

  const title = document.createElement('div');
  title.className = 'source-title';
  title.textContent = source.title || '未知来源';

  const score = document.createElement('span');
  score.className = 'source-score';
  score.textContent = (parseFloat(source.score) || 0).toFixed(2);

  header.append(title, score);
  card.appendChild(header);

  if (source.author) {
    const author = document.createElement('div');
    author.className = 'source-author';
    author.textContent = source.author;
    card.appendChild(author);
  }

  if (source.excerpt) {
    const excerpt = document.createElement('div');
    excerpt.className = 'source-excerpt';
    excerpt.textContent = source.excerpt;
    card.appendChild(excerpt);
  }

  if (source.doc_uuid) {
    const hint = document.createElement('div');
    hint.className = 'source-jump-hint';
    const hintIcon = document.createElement('i');
    hintIcon.setAttribute('data-lucide', 'book-open-text');
    const hintText = document.createElement('span');
    hintText.textContent = '双击跳转原文';
    hint.append(hintIcon, hintText);
    card.appendChild(hint);
    card.classList.add('jumpable');
  }

  // 单击展开详情，双击跳原文。延迟执行单击，否则双击时会先把
  // 详情面板展开一次，视觉上闪一下
  let clickTimer = null;
  card.addEventListener('click', () => {
    if (clickTimer) return;
    clickTimer = setTimeout(() => {
      clickTimer = null;
      toggleSourceDetail(card, source);
    }, 250);
  });
  card.addEventListener('dblclick', (e) => {
    e.preventDefault();
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
    jumpToSource(source);
  });
  return card;
}

/**
 * 展开/收起来源卡片的详情面板。
 * @param {HTMLElement} card - 来源卡片
 * @param {Object} source - 来源数据
 */
function toggleSourceDetail(card, source) {
  const existing = $('.source-detail-panel', card);
  if (existing) { existing.remove(); return; }

  // 同一时刻只展开一张
  $$('.source-detail-panel').forEach((p) => p.remove());

  const panel = document.createElement('div');
  panel.className = 'source-detail-panel visible';

  const excerpt = document.createElement('div');
  excerpt.className = 'detail-excerpt';
  excerpt.textContent = source.excerpt || '';
  panel.appendChild(excerpt);

  const meta = document.createElement('div');
  meta.className = 'detail-meta';
  if (source.author) {
    const authorSpan = document.createElement('span');
    authorSpan.textContent = '作者: ' + source.author;
    meta.appendChild(authorSpan);
  }
  const scoreSpan = document.createElement('span');
  scoreSpan.textContent = '相关度: ' + (parseFloat(source.score) || 0).toFixed(2);
  meta.appendChild(scoreSpan);

  if (source.source_url) {
    const linkWrap = document.createElement('span');
    const link = document.createElement('a');
    link.href = source.source_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = '查看原文';
    link.style.color = 'var(--color-text-link)';
    linkWrap.appendChild(link);
    meta.appendChild(linkWrap);
  }
  panel.appendChild(meta);

  const collapseBtn = document.createElement('button');
  collapseBtn.type = 'button';
  collapseBtn.className = 'detail-collapse-btn';
  collapseBtn.innerHTML = '<i data-lucide="chevron-up"></i> 收起';
  collapseBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    panel.remove();
  });
  panel.appendChild(collapseBtn);

  card.appendChild(panel);
  refreshIcons();
}

/**
 * 给助手消息挂上操作按钮。
 * @param {HTMLElement} row - 消息行
 * @param {string} answerText - 回答全文，供复制使用
 * @param {Function} onRegenerate - 重新生成回调
 * @param {HTMLElement} [variantSwitch] - 版本切换器，没有多版本时不传
 */
export function appendMessageActions(row, answerText, onRegenerate, variantSwitch) {
  const body = $('.msg-body', row);
  const existing = $('.msg-actions', body);
  if (existing) existing.remove();

  const actions = document.createElement('div');
  actions.className = 'msg-actions';

  // 切换器放在最左，和图示的排布一致
  if (variantSwitch) actions.appendChild(variantSwitch);

  const copyBtn = createIconButton('copy', '复制', 'msg-action-btn');
  copyBtn.addEventListener('click', () => copyToClipboard(answerText, copyBtn));

  const regenBtn = createIconButton('refresh-cw', '重新生成', 'msg-action-btn');
  regenBtn.addEventListener('click', onRegenerate);

  actions.append(copyBtn, regenBtn);
  body.appendChild(actions);
  refreshIcons();
}

/**
 * 创建版本切换器 `< n/m >`。
 *
 * 同一个提问改过多次、或回答重新生成过，后端会把它们存成兄弟版本。
 * 没有这个控件，改完提问旧版本就再也找不回来了。
 *
 * @param {Object} msg - 消息对象，需含 id / variant_count / variant_index
 * @param {Function} onSwitch - 切换回调，签名 (direction) => void，
 *                              direction 为 -1（上一个）或 +1（下一个）
 * @returns {HTMLElement|null} - variant_count <= 1 时返回 null
 */
export function createVariantSwitch(msg, onSwitch) {
  const count = msg.variant_count || 1;
  // 只有一个版本时不显示，避免每条消息都挂个无意义的 1/1
  if (count <= 1) return null;

  const index = msg.variant_index || 0;

  const box = document.createElement('div');
  box.className = 'msg-variant-switch';

  const prev = createIconButton('chevron-left', '上一个版本', 'msg-variant-btn');
  prev.disabled = index <= 0;
  prev.addEventListener('click', () => onSwitch(-1));

  const label = document.createElement('span');
  label.className = 'msg-variant-label';
  label.textContent = `${index + 1} / ${count}`;

  const next = createIconButton('chevron-right', '下一个版本', 'msg-variant-btn');
  next.disabled = index >= count - 1;
  next.addEventListener('click', () => onSwitch(1));

  box.append(prev, label, next);
  return box;
}

/**
 * 把消息的树结构字段挂到 DOM 上，后续操作（编辑、重新生成、切版本）都要用。
 * @param {HTMLElement} row - 消息行
 * @param {Object} msg - 后端返回的消息对象
 */
export function setRowMessageMeta(row, msg) {
  if (!msg) return;
  if (msg.id != null) row.dataset.msgId = String(msg.id);
  // parent_id 为 null 是合法值（根消息），要能和"没有这个字段"区分开，
  // 所以存成字符串 "null" 而不是干脆不写
  row.dataset.parentId = msg.parent_id == null ? 'null' : String(msg.parent_id);
  row.dataset.variantCount = String(msg.variant_count || 1);
  row.dataset.variantIndex = String(msg.variant_index || 0);
}

/**
 * 读取消息行上的树结构字段。
 * @param {HTMLElement} row - 消息行
 * @returns {Object} - { id, parentId, variantCount, variantIndex }
 */
export function getRowMessageMeta(row) {
  const raw = row.dataset.parentId;
  return {
    id: row.dataset.msgId ? Number(row.dataset.msgId) : null,
    // "null" 表示这是根消息；undefined 表示这行还没绑定过后端消息
    parentId: raw === 'null' ? null : (raw === undefined ? undefined : Number(raw)),
    variantCount: Number(row.dataset.variantCount || 1),
    variantIndex: Number(row.dataset.variantIndex || 0),
  };
}

/**
 * 复制文本到剪贴板，并给按钮做短暂的成功反馈。
 * @param {string} text - 待复制文本
 * @param {HTMLElement} btn - 触发按钮
 */
async function copyToClipboard(text, btn) {
  const showSuccess = () => {
    btn.innerHTML = '<i data-lucide="check"></i>';
    refreshIcons();
    setTimeout(() => {
      btn.innerHTML = '<i data-lucide="copy"></i>';
      refreshIcons();
    }, 2000);
  };

  try {
    await navigator.clipboard.writeText(text);
    showSuccess();
  } catch {
    // http（非 https）环境下 Clipboard API 不可用，退回旧接口
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      showSuccess();
    } catch {
      console.error('复制失败');
    }
    document.body.removeChild(ta);
  }
}

/**
 * 用净化后的 Markdown 更新消息文本区。
 *
 * 正文写进独立的 .msg-answer 容器，思考面板与搜索面板留在它外面。
 * 早先的做法是直接改 msgText.innerHTML、再把面板插回最前面，
 * 流式输出每 50ms 重绘一次，面板就跟着被拔出重插——表现为
 * 深度思考面板持续闪烁，展开状态也会在重建时丢掉，
 * 于是"有时候点不开"。
 *
 * @param {HTMLElement} msgText - .msg-text 元素
 * @param {string} markdown - Markdown 源文本
 * @param {Array<HTMLElement>} panels - 需要保留在顶部的面板（思考、搜索）
 */
export function renderAnswer(msgText, markdown, panels) {
  const list = panels.filter(Boolean);
  // 面板要排在正文前面，且顺序固定，避免每轮重绘位置跳动
  list.forEach((panel, i) => {
    if (panel.parentNode !== msgText) {
      msgText.insertBefore(panel, msgText.children[i] || null);
    }
  });

  let answer = $('.msg-answer', msgText);
  if (!answer) {
    answer = document.createElement('div');
    answer.className = 'msg-answer';
    msgText.appendChild(answer);
  }
  // 只重绘正文这一块，面板的 DOM 完全不动
  answer.innerHTML = renderMarkdown(markdown);
  hardenLinks(answer);
  refreshIcons();
}

/**
 * 重建左侧对话大纲。
 * @param {HTMLElement} timeline - 大纲容器
 * @param {HTMLElement} container - 会话容器
 * @param {HTMLElement} scrollEl - 滚动容器
 * @returns {Array<{item: HTMLElement, row: HTMLElement}>} - 大纲项与消息行的对应关系
 */
export function updateTimeline(timeline, container, scrollEl) {
  timeline.innerHTML = '';
  const userRows = $$('.message-row.user-message', container);

  if (!userRows.length) {
    timeline.classList.remove('visible');
    return [];
  }
  timeline.classList.add('visible');

  const label = document.createElement('div');
  label.className = 'v-timeline-label';
  label.textContent = '对话大纲';
  timeline.appendChild(label);

  const dots = [];
  userRows.forEach((row) => {
    const text = getRowOriginalText(row);

    const item = document.createElement('div');
    item.className = 'v-timeline-item';
    item.tabIndex = 0;
    item.setAttribute('role', 'button');

    const dot = document.createElement('span');
    dot.className = 'v-timeline-dot';
    const textSpan = document.createElement('span');
    textSpan.className = 'v-timeline-text';
    textSpan.textContent = text;
    textSpan.title = text;

    item.append(dot, textSpan);

    const jump = () => row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    item.addEventListener('click', jump);
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jump(); }
    });

    timeline.appendChild(item);
    dots.push({ item, row });
  });

  return dots;
}

/**
 * 根据滚动位置高亮大纲中当前所处的节点。
 * @param {Array<{item: HTMLElement, row: HTMLElement}>} dots - 大纲项
 * @param {HTMLElement} scrollEl - 滚动容器
 */
export function highlightTimeline(dots, scrollEl) {
  if (!dots.length) return;
  const rect = scrollEl.getBoundingClientRect();
  const viewMid = rect.height / 2;

  let activeIdx = 0;
  for (let i = dots.length - 1; i >= 0; i--) {
    const rowRect = dots[i].row.getBoundingClientRect();
    const rowMid = rowRect.top - rect.top + rowRect.height / 2;
    if (rowMid <= viewMid) { activeIdx = i; break; }
  }
  dots.forEach((d, idx) => d.item.classList.toggle('active', idx === activeIdx));
}

/**
 * 删除指定消息行之后的所有消息（编辑/重新生成时的截断）。
 * @param {HTMLElement} container - 会话容器
 * @param {HTMLElement} row - 保留到这一行（含）
 */
export function truncateAfter(container, row) {
  const rows = $$('.message-row', container);
  const idx = rows.indexOf(row);
  if (idx === -1) return;
  for (let i = rows.length - 1; i > idx; i--) rows[i].remove();
}
