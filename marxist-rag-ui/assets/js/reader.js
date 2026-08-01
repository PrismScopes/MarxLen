/**
 * reader.js —— 原文阅读器
 *
 * 向量库里的片段是检索用的碎片，拼不出连续正文，所以正文直接由后端读
 * 原始 Markdown 返回，本模块只负责渲染、翻页与定位。
 *
 * 两个入口：
 *   1. 切到"原文查询"模式 —— 打开阅读器，从书目开始选
 *   2. 双击来源卡片 —— 打开阅读器并跳到该片段在原文中的位置
 */

import * as api from './api.js';
import { $, refreshIcons } from './dom-utils.js';
import { STORAGE_KEYS, READER_HIGHLIGHT_MS } from './config.js';
import { state as appState } from './store.js';
import { addQuote } from './quote.js';

/** 阅读器运行时状态 */
const state = {
  source: '',        // 当前打开的文件名
  nextSeq: 0,        // 下一页的起始段落序号
  eof: false,        // 是否已读到结尾
  loading: false,    // 正在取下一页，防重入
  total: 0,          // 总段落数
  scope: 'current',  // 检索范围：current | all
  searchAbort: null, // 进行中的检索，换查询时用它掐掉上一次
};

let refs = null;

/**
 * 初始化阅读器，绑定一次性事件。
 * @returns {Object} - 阅读器的 DOM 引用集合
 */
export function initReader() {
  refs = {
    view: $('#readerView'),
    title: $('#readerTitle'),
    backBtn: $('#readerBackBtn'),
    filter: $('#readerFilter'),
    bookList: $('#readerBookList'),
    tocList: $('#readerToc'),
    content: $('#readerContent'),
    status: $('#readerStatus'),
    tocToggle: $('#readerTocToggle'),
    searchToggle: $('#readerSearchToggle'),
    searchInput: $('#readerSearchInput'),
    searchMeta: $('#readerSearchMeta'),
    searchResults: $('#readerSearchResults'),
    selectionTip: $('#readerSelectionTip'),
    quoteBtn: $('#readerQuoteBtn'),
  };
  if (!refs.view) return null;

  refs.backBtn.addEventListener('click', showBookList);

  // 滚到底部继续加载：正文可能有上万段，一次性渲染会卡死
  refs.content.addEventListener('scroll', () => {
    const { scrollTop, scrollHeight, clientHeight } = refs.content;
    if (scrollHeight - scrollTop - clientHeight < 600) loadMore();
  });

  initPaneToggles();
  initFilter();
  initSearchPane();
  initSelectionQuote();

  return refs;
}

/**
 * 两侧栏折叠。
 * 检索窗口在 1024px 以下默认收起，那档 CSS 用 .show-search 展开；
 * 宽屏则默认展开、用 .hide-search 收起。两个类始终成对设置，
 * 否则跨过断点时会出现"两个类都不生效、栏宽回到默认"的错乱。
 */
function initPaneToggles() {
  refs.tocToggle.addEventListener('click', () => {
    refs.view.classList.toggle('hide-toc');
  });

  let searchOpen = window.innerWidth > 1024;
  const applySearch = () => {
    refs.view.classList.toggle('hide-search', !searchOpen);
    refs.view.classList.toggle('show-search', searchOpen);
  };
  applySearch();

  refs.searchToggle.addEventListener('click', () => {
    searchOpen = !searchOpen;
    applySearch();
    if (searchOpen) refs.searchInput.focus();
  });
}

/**
 * 左栏筛选框：书目态过滤文献名，阅读态过滤章节名。
 * 纯前端过滤，不发请求——书目 135 条、目录几百条，本地筛完全够快。
 */
function initFilter() {
  refs.filter.addEventListener('input', () => {
    const kw = refs.filter.value.trim().toLowerCase();
    const reading = refs.view.classList.contains('reading');
    const items = (reading ? refs.tocList : refs.bookList).children;
    for (const el of items) {
      const hit = !kw || el.textContent.toLowerCase().includes(kw);
      el.style.display = hit ? '' : 'none';
    }
  });
}

/**
 * 进入阅读器视图。
 * 阅读器不是浮层，而是"原文查询"模式的主视图，
 * 靠 body 上的 mode-original 类与消息区、输入区互斥显示。
 */
function openPanel() {
  document.body.classList.add('mode-original');
  appState.currentMode = 'original';
  // 从来源卡片跳进来时，模式条还停在原来的问答模式上，
  // 这里把它同步过去，避免"界面是阅读器、tab 却显示通用问答"。
  document.querySelectorAll('.mode-tab').forEach((t) => {
    const isOriginal = t.dataset.mode === 'original';
    t.classList.toggle('active', isOriginal);
    t.setAttribute('aria-selected', isOriginal ? 'true' : 'false');
  });
}

/** 退出阅读器视图，回到对话界面 */
export function closeReader() {
  document.body.classList.remove('mode-original');
  // 划词按钮是 fixed 定位的，不收起会留在对话界面上
  hideSelectionTip();
}

/**
 * 打开阅读器。有上次阅读记录则续读，否则显示书目。
 */
export async function openReader() {
  if (!refs) return;
  openPanel();
  const last = readLastPosition();
  if (last && last.source) {
    await openBook(last.source, last.seq || 0);
  } else {
    await showBookList();
  }
}

/**
 * 从来源卡片跳转：定位片段并打开对应位置。
 * @param {Object} source - 来源数据，需含 doc_uuid
 */
export async function jumpToSource(source) {
  if (!refs) return;
  if (!source || !source.doc_uuid) {
    // 没有 uuid 说明这条来源来自旧的缓存回答，只能退到书目
    openPanel();
    await showBookList();
    setStatus('这条来源没有记录原文位置，请从书目中查找', 'warn');
    return;
  }

  openPanel();
  setStatus('正在定位原文...');

  let loc;
  try {
    loc = await api.locateChunk(source.doc_uuid);
  } catch (err) {
    setStatus(`定位失败：${err.message}`, 'error');
    return;
  }

  // 目标段落可能在很靠后的位置，从它往前一点开始加载，
  // 让用户能看到上下文而不是从中间突兀开始
  const startSeq = Math.max(0, (loc.seq || 0) - 3);
  await openBook(loc.source, startSeq, loc.seq);

  if (!loc.matched) {
    setStatus(
      loc.fallback === 'chapter'
        ? '未能精确定位到该段，已跳转到所属章节'
        : '未能在原文中定位到该段，已为你打开该文献',
      'warn',
    );
  } else if (loc.ambiguous) {
    setStatus('这段文字在原文中出现多次，已跳转到第一处', 'warn');
  } else {
    setStatus('');
  }
}

/**
 * 渲染书目态下正文区的引导空态。
 * 书目列表在左栏，右栏没有正文，留白太大会显得像加载失败。
 */
function renderEmptyHint() {
  const box = document.createElement('div');
  box.className = 'reader-empty';

  const icon = document.createElement('i');
  icon.setAttribute('data-lucide', 'book-open');

  const title = document.createElement('div');
  title.className = 'reader-empty-title';
  title.textContent = '从左侧选择一部文献';

  const desc = document.createElement('div');
  desc.className = 'reader-empty-desc';
  desc.textContent = '也可以在问答的来源卡片上双击，直接跳到原文对应位置';

  box.append(icon, title, desc);
  refs.content.appendChild(box);
  refreshIcons();
}

/** 显示书目列表 */
async function showBookList() {
  refs.view.classList.remove('reading');
  refs.title.textContent = '原文书目';
  refs.content.innerHTML = '';
  refs.tocList.innerHTML = '';
  state.source = '';
  // 筛选框内容是给目录用的，回到书目要清掉，
  // 否则书目会被上一本书的章节关键词过滤成空列表
  refs.filter.value = '';
  refs.filter.placeholder = '搜索书名...';
  for (const el of refs.bookList.children) el.style.display = '';
  // 书目在左栏，右栏此时无正文可显示，给个引导避免大片空白
  renderEmptyHint();

  if (refs.bookList.childElementCount) return;  // 书目只拉一次

  setStatus('正在加载书目...');
  let data;
  try {
    data = await api.listBooks();
  } catch (err) {
    setStatus(`加载书目失败：${err.message}`, 'error');
    return;
  }

  refs.bookList.innerHTML = '';
  data.books.forEach((b) => {
    const item = document.createElement('div');
    item.className = 'reader-book-item';
    item.tabIndex = 0;
    item.setAttribute('role', 'button');

    const name = document.createElement('div');
    name.className = 'reader-book-name';
    name.textContent = b.title;
    name.title = b.title;

    const size = document.createElement('span');
    size.className = 'reader-book-size';
    size.textContent = b.size_kb > 1024
      ? `${(b.size_kb / 1024).toFixed(1)} MB`
      : `${Math.round(b.size_kb)} KB`;

    item.append(name, size);
    const open = () => openBook(b.source, 0);
    item.addEventListener('click', open);
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    refs.bookList.appendChild(item);
  });

  setStatus(`共 ${data.total} 部文献`);
}

/**
 * 打开一本书。
 * @param {string} source - 文件名
 * @param {number} startSeq - 从第几段开始加载
 * @param {number} [highlightSeq] - 需要高亮定位的段落序号
 */
async function openBook(source, startSeq = 0, highlightSeq = -1) {
  refs.view.classList.add('reading');
  refs.title.textContent = source.replace(/\.md$/, '');
  refs.content.innerHTML = '';
  refs.content.scrollTop = 0;
  // 换书了，上一本的章节筛选词留着没有意义
  refs.filter.value = '';
  refs.filter.placeholder = '搜索章节...';

  state.source = source;
  state.nextSeq = startSeq;
  state.eof = false;
  state.loading = false;

  loadToc(source);

  const ok = await loadMore();
  if (ok && highlightSeq >= 0) highlightParagraph(highlightSeq);
  saveLastPosition(source, startSeq);
}

/** 加载目录（失败不影响正文阅读，静默降级） */
async function loadToc(source) {
  refs.tocList.innerHTML = '';
  let data;
  try {
    data = await api.getToc(source);
  } catch {
    return;
  }
  state.total = data.total_paragraphs;

  data.toc.forEach((entry) => {
    const item = document.createElement('div');
    item.className = `reader-toc-item level-${entry.level}`;
    item.textContent = entry.text;
    item.title = entry.text;
    item.tabIndex = 0;
    item.setAttribute('role', 'button');
    const jump = () => openBook(source, entry.seq, entry.seq);
    item.addEventListener('click', jump);
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); jump(); }
    });
    refs.tocList.appendChild(item);
  });
}

/**
 * 加载下一页正文。
 * @returns {Promise<boolean>} - 是否成功
 */
async function loadMore() {
  if (state.loading || state.eof || !state.source) return false;
  state.loading = true;

  let data;
  try {
    data = await api.getContent(state.source, state.nextSeq);
  } catch (err) {
    setStatus(`加载正文失败：${err.message}`, 'error');
    state.loading = false;
    return false;
  }

  const frag = document.createDocumentFragment();
  data.paragraphs.forEach((p) => {
    const el = document.createElement(p.heading ? 'h3' : 'p');
    el.className = p.heading ? 'reader-heading' : 'reader-para';
    el.dataset.seq = p.seq;
    // 用 textContent 而非 innerHTML：正文是 OCR 产物，可能含尖括号等
    // 字符，按 HTML 解析会破坏排版甚至引入注入风险
    el.textContent = p.heading ? p.text.replace(/^#{1,4}\s+/, '') : p.text;
    frag.appendChild(el);
  });
  refs.content.appendChild(frag);

  state.nextSeq = data.next_seq;
  state.eof = data.eof;
  state.total = data.total_paragraphs;
  state.loading = false;

  if (state.eof) {
    const end = document.createElement('div');
    end.className = 'reader-end';
    end.textContent = '— 全文完 —';
    refs.content.appendChild(end);
  }
  saveLastPosition(state.source, data.seq);
  return true;
}

/**
 * 滚动到指定段落并高亮。
 * @param {number} seq - 段落序号
 */
function highlightParagraph(seq) {
  const el = refs.content.querySelector(`[data-seq="${seq}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('reader-hit');
  // 高亮只是引导视线，留着会干扰后续阅读
  setTimeout(() => el.classList.remove('reader-hit'), READER_HIGHLIGHT_MS);
}

/**
 * 更新状态栏文案。
 * @param {string} text - 文案，空串表示清除
 * @param {string} [level] - "warn" | "error"
 */
function setStatus(text, level = '') {
  refs.status.textContent = text;
  refs.status.className = 'reader-status' + (level ? ` ${level}` : '');
}

/** 记住阅读位置，下次打开直接续读 */
function saveLastPosition(source, seq) {
  try {
    localStorage.setItem(STORAGE_KEYS.readerLast, JSON.stringify({ source, seq }));
  } catch {
    /* 隐私模式下 localStorage 不可写，续读功能降级，不影响阅读 */
  }
}

/** 读取上次阅读位置 */
function readLastPosition() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEYS.readerLast) || 'null');
  } catch {
    return null;
  }
}

// ── 检索窗口 ────────────────────────────────────────────────

/** 绑定检索窗口的输入与范围切换 */
function initSearchPane() {
  refs.searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      runSearch();
    }
  });

  refs.view.querySelectorAll('.reader-scope-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.scope = btn.dataset.scope;
      refs.view.querySelectorAll('.reader-scope-btn').forEach((b) => {
        const on = b === btn;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', on ? 'true' : 'false');
      });
      // 范围变了，已有结果就过时了，重搜一次省得用户再按一次回车
      if (refs.searchInput.value.trim()) runSearch();
    });
  });
}

/** 执行一次原文检索 */
async function runSearch() {
  const query = refs.searchInput.value.trim();
  if (!query) return;

  // 限定当前书却还没打开任何书，先提示而不是让后端报 400
  if (state.scope === 'current' && !state.source) {
    setSearchMeta('请先打开一部文献，或把范围切到「全部文献」', true);
    return;
  }

  // 上一次检索还没回来就换了词，掐掉它：LLM 抽词要几秒，
  // 不中断的话旧结果可能后到，覆盖掉新结果
  if (state.searchAbort) state.searchAbort.abort();
  const ctrl = new AbortController();
  state.searchAbort = ctrl;

  refs.searchResults.innerHTML = '';
  setSearchMeta('正在检索...');

  let data;
  try {
    data = await api.searchReader(query, state.scope, state.source, ctrl.signal);
  } catch (err) {
    if (err.name === 'AbortError') return;  // 被新的检索取代，不报错
    setSearchMeta(`检索失败：${err.message}`, true);
    return;
  } finally {
    if (state.searchAbort === ctrl) state.searchAbort = null;
  }

  renderSearchResults(data);
}

/**
 * 渲染检索结果。
 * @param {Object} data - 后端返回，含 results / keywords / llm_ok
 */
function renderSearchResults(data) {
  const results = data.results || [];
  if (!results.length) {
    setSearchMeta('没有找到相关内容，换个说法试试');
    return;
  }

  // 把 LLM 抽出的关键词显示出来，让用户知道模型把描述理解成了什么。
  // llm_ok 为 false 说明退回了原始描述检索，结果会差一些，要讲清楚。
  const kw = (data.keywords || []).join('、');
  setSearchMeta(
    data.llm_ok
      ? (kw ? `关键词：${kw}` : `共 ${results.length} 条`)
      : '模型不可用，已按原文描述直接检索',
  );

  const frag = document.createDocumentFragment();
  results.forEach((r) => {
    const item = document.createElement('div');
    item.className = 'reader-result-item';
    item.tabIndex = 0;
    item.setAttribute('role', 'button');

    const title = document.createElement('div');
    title.className = 'reader-result-title';
    title.textContent = r.chapter ? `${r.title} · ${r.chapter}` : r.title;
    title.title = title.textContent;

    const excerpt = document.createElement('div');
    excerpt.className = 'reader-result-excerpt';
    excerpt.textContent = r.excerpt;

    item.append(title, excerpt);

    const go = () => {
      // 后端已经把位置算好了，seq >= 0 就能直接跳，不用再调 locate
      if (r.seq >= 0) {
        openBook(r.source, Math.max(0, r.seq - 3), r.seq);
      } else {
        openBook(r.source, 0);
        setStatus('未能定位到具体段落，已打开该文献', 'warn');
      }
    };
    item.addEventListener('click', go);
    item.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
    frag.appendChild(item);
  });
  refs.searchResults.appendChild(frag);
}

/**
 * 更新检索窗口的提示行。
 * @param {string} text - 文案
 * @param {boolean} [isError] - 是否按错误样式显示
 */
function setSearchMeta(text, isError = false) {
  refs.searchMeta.textContent = text;
  refs.searchMeta.className = 'reader-search-meta' + (isError ? ' error' : '');
}

// ── 划词引用 ────────────────────────────────────────────────

/**
 * 划词后浮出"添加到对话框"，把选中原文送进下方输入框。
 *
 * 这是原文模式与另外两个问答模式之间的桥：读到一段想深究，
 * 划中它、点一下，再切到通用问答或马哲方法论直接提问。
 */
function initSelectionQuote() {
  // 用 mouseup 而不是 selectionchange：后者在拖选过程中会连续触发，
  // 按钮会跟着鼠标乱跳
  refs.content.addEventListener('mouseup', () => {
    // 等一帧再读选区，否则拿到的还是这次点击之前的旧选区
    requestAnimationFrame(showSelectionTip);
  });

  // 点空白、滚动、切模式都要收起按钮，否则它会飘在无关的位置上
  document.addEventListener('mousedown', (e) => {
    if (!refs.selectionTip.contains(e.target)) hideSelectionTip();
  });
  refs.content.addEventListener('scroll', hideSelectionTip);

  refs.quoteBtn.addEventListener('click', quoteSelection);
}

/** 把浮动按钮定位到选区上方 */
function showSelectionTip() {
  const sel = window.getSelection();
  const text = sel ? sel.toString().trim() : '';
  if (!text) {
    hideSelectionTip();
    return;
  }

  const rect = sel.getRangeAt(0).getBoundingClientRect();
  if (!rect.width && !rect.height) return;

  refs.selectionTip.hidden = false;
  // 先显示再量尺寸：hidden 状态下 offsetWidth 是 0
  const tipW = refs.selectionTip.offsetWidth;
  const tipH = refs.selectionTip.offsetHeight;

  // 贴选区上沿，越过视口顶部时翻到下沿
  let top = rect.top - tipH - 8;
  if (top < 8) top = rect.bottom + 8;
  let left = rect.left + rect.width / 2 - tipW / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - tipW - 8));

  refs.selectionTip.style.left = `${left}px`;
  refs.selectionTip.style.top = `${top}px`;
  refreshIcons();
}

/** 收起浮动按钮 */
function hideSelectionTip() {
  if (refs && refs.selectionTip) refs.selectionTip.hidden = true;
}

/** 把当前选区作为引用卡片挂到输入框上方 */
function quoteSelection() {
  const sel = window.getSelection();
  const text = sel ? sel.toString().trim() : '';
  if (!text) return;

  // 正文里的换行压成空格：原文分段在引用里没有意义，
  // 留着会让卡片的 title 提示排版很乱
  const clean = text.replace(/\s+/g, ' ');
  addQuote(clean, state.source.replace(/\.md$/, ''));

  sel.removeAllRanges();
  hideSelectionTip();
  setStatus('已添加到对话框，切换到问答模式即可提问');
}
