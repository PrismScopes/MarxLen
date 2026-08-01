/**
 * quote.js —— 输入框的引用附件
 *
 * 原文模式下划词"添加到对话框"的落点。早先的做法是把选中的原文
 * 直接拼进 textarea，几百字的引用会把输入框顶满、用户自己要打的字
 * 反而看不见。改成缩略卡片：输入框上方挂一枚带出处的小标签，
 * 正文存在内存里，发送时才拼进问题文本。
 */

import { $, refreshIcons } from './dom-utils.js';

/** 已添加的引用，发送时按顺序拼进问题 */
const quotes = [];

let bar = null;

/** 初始化引用条，绑定容器 */
export function initQuoteBar() {
  bar = $('#chatQuoteBar');
  return bar;
}

/**
 * 添加一条引用。
 * @param {string} text - 选中的原文
 * @param {string} source - 出处书名，可为空
 */
export function addQuote(text, source) {
  if (!bar || !text) return;
  quotes.push({ text, source });
  render();
}

/** 清空全部引用（发送后调用） */
export function clearQuotes() {
  quotes.length = 0;
  render();
}

/** 当前是否有引用 */
export function hasQuotes() {
  return quotes.length > 0;
}

/**
 * 把引用拼成发送用的文本，附在用户问题前面。
 * 带上书名，模型才知道这段话出自哪里，否则容易当成用户自己的表述。
 * @param {string} question - 用户输入的问题
 * @returns {string}
 */
export function composeWithQuotes(question) {
  if (!quotes.length) return question;
  const blocks = quotes.map(({ text, source }) => (
    source ? `引用《${source}》：\n"${text}"` : `引用原文：\n"${text}"`
  ));
  return `${blocks.join('\n\n')}\n\n${question}`;
}

/** 重绘引用条 */
function render() {
  if (!bar) return;
  bar.innerHTML = '';

  quotes.forEach((q, i) => {
    const chip = document.createElement('div');
    chip.className = 'chat-quote-chip';
    // 悬停看全文：卡片上只放得下出处，正文要留个查看的途径
    chip.title = q.text;

    const icon = document.createElement('i');
    icon.setAttribute('data-lucide', 'quote');

    const label = document.createElement('span');
    label.className = 'chat-quote-label';
    // 没有书名时退回正文前 12 字，总得让用户认出是哪一条
    label.textContent = q.source || `${q.text.slice(0, 12)}...`;

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'chat-quote-remove';
    remove.setAttribute('aria-label', '移除这条引用');
    const x = document.createElement('i');
    x.setAttribute('data-lucide', 'x');
    remove.appendChild(x);
    remove.addEventListener('click', () => {
      quotes.splice(i, 1);
      render();
    });

    chip.append(icon, label, remove);
    bar.appendChild(chip);
  });

  refreshIcons();
}
