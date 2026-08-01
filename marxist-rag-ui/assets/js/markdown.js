/**
 * markdown.js —— Markdown 渲染与 HTML 净化
 *
 * 原实现直接 marked.parse() 后塞进 innerHTML，且显式设了
 * sanitize: false（该选项在新版 marked 中已被移除，写了也无效），
 * 注释却声称"通过 DOM 做净化"——实际什么都没做。
 *
 * 渲染内容来自大模型输出和检索到的语料，任一环节被污染
 * （比如语料里混进 <img src=x onerror=...>）就能执行任意脚本，
 * 进而读取 localStorage、调用同源 API。
 *
 * 这里接入 DOMPurify 做真正的净化。
 */

/**
 * 净化 HTML 字符串，移除脚本、事件处理器等危险内容。
 *
 * @param {string} html - 待净化的 HTML
 * @returns {string} - 安全的 HTML
 */
function sanitize(html) {
  if (window.DOMPurify) {
    return window.DOMPurify.sanitize(html, {
      // 允许 Markdown 常用标签 + 表格 + 代码块
      ALLOWED_TAGS: [
        'p', 'br', 'hr', 'span', 'div',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'strong', 'em', 'b', 'i', 'u', 's', 'del', 'mark',
        'ul', 'ol', 'li',
        'blockquote', 'pre', 'code',
        'a', 'img',
        'table', 'thead', 'tbody', 'tr', 'th', 'td',
      ],
      ALLOWED_ATTR: ['href', 'title', 'target', 'rel', 'src', 'alt', 'class'],
      // 只放行安全协议，挡掉 javascript: 和 data: 伪协议
      ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.\-:]|$))/i,
    });
  }

  // DOMPurify 未加载时的降级：整段转义成纯文本。
  // 宁可显示原始标记，也不执行未知脚本。
  console.warn('[markdown] DOMPurify 未加载，已降级为纯文本显示');
  const div = document.createElement('div');
  div.textContent = html;
  return div.innerHTML;
}

/**
 * 初始化 marked 的全局选项。应在页面加载后调用一次。
 */
export function initMarkdown() {
  if (!window.marked) {
    console.error('[markdown] marked 未加载');
    return;
  }
  window.marked.setOptions({
    breaks: true,  // 单个换行也渲染成 <br>，符合聊天场景的直觉
    gfm: true,     // GitHub 风格：表格、删除线、任务列表
  });
}

/**
 * 把 Markdown 文本渲染成安全的 HTML。
 * @param {string} text - Markdown 源文本
 * @returns {string} - 净化后的 HTML
 */
export function renderMarkdown(text) {
  if (!text) return '';
  if (!window.marked) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
  return sanitize(window.marked.parse(text));
}

/**
 * 给容器内所有外链补上安全属性。
 *
 * target="_blank" 若不配 rel="noopener"，新页面可以通过
 * window.opener 反向操纵原页面（反向标签劫持）。
 *
 * @param {Element} container - 需要处理的容器元素
 */
export function hardenLinks(container) {
  container.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || '';
    // 只处理外部链接，页内锚点保持原样
    if (/^https?:\/\//i.test(href)) {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
    }
  });
}
