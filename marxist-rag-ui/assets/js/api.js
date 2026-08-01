/**
 * api.js —— 后端接口封装
 *
 * 所有对 /api/* 的请求都集中在这里，统一做三件事：
 *   1. 检查 resp.ok —— 原来的代码只 catch 网络异常，
 *      HTTP 404/405/500 会被当成成功，导致操作静默失败；
 *   2. 抛出带状态码的 ApiError，让调用方能区分错误类型；
 *   3. 收敛 URL 拼接，避免路径散落各处。
 */

import { API_BASE, HISTORY_LIMIT } from './config.js';

/** 带 HTTP 状态码的错误对象 */
export class ApiError extends Error {
  /**
   * @param {string} message - 错误描述
   * @param {number} status - HTTP 状态码，0 表示网络层失败
   */
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/**
 * 统一的 fetch 包装：检查响应状态并解析 JSON。
 * @param {string} path - 相对于 API_BASE 的路径，如 "/models"
 * @param {RequestInit} [options] - fetch 配置
 * @returns {Promise<any>} - 解析后的 JSON
 * @throws {ApiError} - 网络失败或 HTTP 非 2xx 时抛出
 */
async function request(path, options = {}) {
  let resp;
  try {
    resp = await fetch(API_BASE + path, options);
  } catch (err) {
    // fetch 只在网络层失败时 reject（断网、DNS 失败、CORS 拦截）
    throw new ApiError(`网络请求失败: ${err.message}`, 0);
  }

  if (!resp.ok) {
    // 尝试读取后端返回的 detail 字段，拿不到就用状态文本兜底
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* 响应体不是 JSON，保持 statusText */
    }
    throw new ApiError(detail, resp.status);
  }

  // DELETE 等接口可能返回空体
  const text = await resp.text();
  return text ? JSON.parse(text) : null;
}

/**
 * 构造 JSON 请求配置。
 * @param {string} method - HTTP 方法
 * @param {Object} body - 请求体对象
 * @returns {RequestInit}
 */
function jsonBody(method, body) {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
}

// ── 对话相关 ────────────────────────────────────────────────

/**
 * 拉取对话列表。
 * @param {number} [limit] - 最多返回条数
 * @returns {Promise<Array>} - 对话摘要数组
 */
export function listConversations(limit = HISTORY_LIMIT) {
  return request(`/conversations?limit=${limit}`);
}

/**
 * 获取单个对话的完整消息记录。
 * @param {string} convId - 对话 ID
 * @returns {Promise<Object>} - 含 messages 数组的对话详情
 */
export function getConversation(convId) {
  return request(`/conversations/${encodeURIComponent(convId)}`);
}

/**
 * 取某条消息的全部版本。
 *
 * 只想知道"有几个版本"不必调这个接口——对话详情里的 variant_count
 * 就够了。这里主要用来拿到相邻版本的 message_id 以便切换。
 *
 * @param {string} convId - 对话 ID
 * @param {number} msgId - 消息 ID
 * @returns {Promise<Object>} - 含 variants 数组
 */
export function getMessageVariants(convId, msgId) {
  return request(
    `/conversations/${encodeURIComponent(convId)}/messages/${msgId}/variants`,
  );
}

/**
 * 切换到某个版本。
 *
 * 后端会沿着该版本的后续对话一路走到最新一条，所以切回旧版本时
 * 那条分支下的追问会一并恢复。返回切换后的完整详情，
 * 前端直接整体重渲染即可，不用自己算哪些消息该换。
 *
 * @param {string} convId - 对话 ID
 * @param {number} messageId - 要切过去的版本的消息 ID
 * @returns {Promise<Object>} - 切换后的 ConversationDetail
 */
export function switchVariant(convId, messageId) {
  return request(
    `/conversations/${encodeURIComponent(convId)}/switch`,
    jsonBody('POST', { message_id: messageId }),
  );
}

/**
 * 重命名对话。
 * @param {string} convId - 对话 ID
 * @param {string} title - 新标题
 * @returns {Promise<Object>} - 更新后的对话摘要
 */
export function renameConversation(convId, title) {
  return request(`/conversations/${encodeURIComponent(convId)}`, jsonBody('PATCH', { title }));
}

/**
 * 删除对话。
 * @param {string} convId - 对话 ID
 * @returns {Promise<Object>}
 */
export function deleteConversation(convId) {
  return request(`/conversations/${encodeURIComponent(convId)}`, { method: 'DELETE' });
}

// ── 模型与设置 ──────────────────────────────────────────────

/**
 * 获取可用模型列表。
 * @returns {Promise<Array>} - [{ id, name, provider }]
 */
export function listModels() {
  return request('/models');
}

/**
 * 持久化当前选中的模型。
 * @param {string} model - 模型 ID
 * @returns {Promise<Object>}
 */
export function saveModel(model) {
  return request('/settings/model', jsonBody('POST', { model }));
}

/**
 * 读取设置。
 * 后端返回 { categories, items } 两段结构，原样交给调用方。
 * @returns {Promise<{categories: Array, items: Array}>}
 */
export async function getSettings() {
  const data = await request('/settings');
  return {
    categories: data.categories || [],
    items: data.items || [],
  };
}

/**
 * 更新设置项。
 * @param {Object} updates - 形如 { key: value } 的增量更新
 * @returns {Promise<Object>}
 */
export function updateSettings(updates) {
  return request('/settings', jsonBody('PUT', { updates }));
}

/**
 * 获取知识库与缓存统计。
 * @returns {Promise<Object>}
 */
export function getStats() {
  return request('/settings/stats');
}

/**
 * 清除指定类型的缓存。
 * @param {string} cacheType - "embedding" | "answer" | "all"
 * @returns {Promise<Object>}
 */
export function clearCache(cacheType) {
  return request('/settings/cache/clear', jsonBody('POST', { cache_type: cacheType }));
}

// ── 原文阅读器 ──────────────────────────────────────────────

/**
 * 获取书目列表。
 * @returns {Promise<{total: number, books: Array}>}
 */
export function listBooks() {
  return request('/reader/books');
}

/**
 * 获取某本书的标题目录。
 * @param {string} source - 原文文件名
 * @returns {Promise<Object>}
 */
export function getToc(source) {
  return request(`/reader/toc?source=${encodeURIComponent(source)}`);
}

/**
 * 按段落序号取正文。
 * @param {string} source - 原文文件名
 * @param {number} seq - 起始段落序号
 * @returns {Promise<Object>} - 含 paragraphs / next_seq / eof
 */
export function getContent(source, seq = 0) {
  return request(`/reader/content?source=${encodeURIComponent(source)}&seq=${seq}`);
}

/**
 * 把检索片段定位到原文位置。
 * @param {string} docUuid - 片段 uuid
 * @returns {Promise<Object>} - 含 source / seq / matched / ambiguous
 */
export function locateChunk(docUuid) {
  return request(`/reader/locate?doc_uuid=${encodeURIComponent(docUuid)}`);
}

/**
 * 在原文中做语义检索。
 *
 * 用户输入的是"我记得有一段讲……"这类描述，原文里并不存在这串字，
 * 所以后端先用 LLM 转成术语与命题再检索。这是阅读器里唯一会调 LLM 的
 * 接口，可能要几秒，调用方应当传 signal 以便用户改主意时中断。
 *
 * @param {string} query - 自然语言描述
 * @param {string} scope - "current"（仅当前书）或 "all"
 * @param {string} source - scope 为 current 时必传的文件名
 * @param {AbortSignal} [signal] - 用于中断请求
 * @returns {Promise<Object>} - 含 results / keywords / proposition
 */
export function searchReader(query, scope, source, signal) {
  return request('/reader/search', {
    ...jsonBody('POST', { query, scope, source }),
    signal,
  });
}

// ── 聊天流式接口 ────────────────────────────────────────────

/**
 * 发起流式聊天请求。
 *
 * 不用原生 EventSource，因为它只支持 GET 且无法带 body，
 * 而这里需要 POST 提交问题、模式、模型等参数。
 *
 * @param {Object} payload - 请求体
 * @param {AbortSignal} signal - 用于中断请求
 * @returns {Promise<Response>} - 原始 Response，由调用方读取流
 * @throws {ApiError}
 */
export async function postChat(payload, signal) {
  let resp;
  try {
    resp = await fetch(API_BASE + '/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
  } catch (err) {
    // AbortError 要原样抛出，让调用方识别成"用户主动停止"
    if (err.name === 'AbortError') throw err;
    throw new ApiError(`网络请求失败: ${err.message}`, 0);
  }

  if (!resp.ok) {
    throw new ApiError(`API 请求失败: ${resp.status} ${resp.statusText}`, resp.status);
  }
  return resp;
}

/**
 * 解析 SSE 字节流，逐个产出事件。
 *
 * 这里手写解析而非用库，关键处理两点：
 *   1. decoder.decode(chunk, { stream: true }) —— 中文是多字节 UTF-8，
 *      一个汉字可能被 TCP 分包截成两半，必须用流式解码模式；
 *   2. lines.pop() 保留最后一个可能不完整的行到下一轮，
 *      否则半行 JSON 会解析失败被丢弃。
 *
 * @param {ReadableStream} body - Response.body
 * @yields {{ event: string, data: Object }} - 解析出的事件
 */
export async function* parseSSE(body) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let currentEvent = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      // 必须始终解码，不能因为"用户切走了"就跳过：
      // 跳过会丢字节，还会让 TextDecoder 的多字节状态错位产生乱码
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const dataStr = line.slice(6).trim();
          if (!dataStr) continue;
          try {
            yield { event: currentEvent, data: JSON.parse(dataStr) };
          } catch {
            /* 非 JSON 行（SSE 注释、心跳包），忽略 */
          }
          currentEvent = '';
        }
      }
    }
  } finally {
    // 提前 return（如调用方 break）时释放读取锁
    reader.releaseLock();
  }
}
