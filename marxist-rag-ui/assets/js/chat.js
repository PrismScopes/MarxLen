/**
 * chat.js —— 聊天核心流程
 *
 * 负责发起请求、消费 SSE 流、驱动界面更新。
 * 这里修掉了原版两个关键缺陷：
 *   1. 用户切换对话时不再丢弃流数据（只是不写 DOM）；
 *   2. 思考内容与解构卡片改由后端随消息落库（刷新不丢），
 *      不再按"消息序号"存 localStorage。
 */

import * as api from './api.js';
import {
  state, isSwitchedAway,
} from './store.js';
import { STREAM_RENDER } from './config.js';
import { $, $$, refreshIcons, scrollToBottom, isNearBottom } from './dom-utils.js';
import { openReader } from './reader.js';
import { composeWithQuotes, clearQuotes, hasQuotes } from './quote.js';
import {
  createUserMessage, createAssistantMessage, createThinkingPanel,
  createSearchRefsPanel, fillSearchRefs, createLoadingIndicator,
  updateLoadingStage, collapseLoadingStages, createStageDetail,
  appendSources, appendMessageActions, renderAnswer, showTimings,
  restoreUserMessage, enterEditMode, truncateAfter,
  setRowOriginalText, getRowOriginalText,
  createVariantSwitch, setRowMessageMeta, getRowMessageMeta,
} from './messages.js';

/**
 * 创建聊天控制器。
 * @param {Object} refs - DOM 引用集合
 * @param {Object} callbacks - 外部回调集合
 * @returns {Object} - 控制器实例
 */
export function createChatController(refs, callbacks) {
  const {
    chatMessages, welcomeContainer, conversationContainer,
    textarea, sendBtn, stopBtn,
  } = refs;

  /**
   * 正在流式生成的那一轮的 DOM 与文本快照。
   *
   * 用户切到别的对话再切回来时，助手回答还没落库（后端只在流结束时
   * add_message），只靠 renderConversation 拉后端数据会看不到正在生成的
   * 内容——表现为"切回来内容丢失，等渲染完才出现"。
   * 把这一轮的节点留在这里，切回时原样接回去。
   */
  let activeStream = null;

  /**
   * 切换发送/停止按钮的显示状态。
   * @param {boolean} streaming - 是否处于生成中
   */
  function setStreamingUI(streaming) {
    state.isStreaming = streaming;
    sendBtn.style.display = streaming ? 'none' : 'flex';
    stopBtn.style.display = streaming ? 'flex' : 'none';
    if (!streaming) {
      textarea.disabled = false;
      sendBtn.disabled = !textarea.value.trim();
    }
  }

  /**
   * 进入会话视图（隐藏欢迎屏）。
   */
  function showConversationView() {
    welcomeContainer.style.display = 'none';
    conversationContainer.style.display = 'block';
  }

  /**
   * 处理用户消息的编辑动作。
   *
   * 关键改动：不再把 currentConversationId 置空。以前置空是为了让后端
   * 新建对话、避免覆盖旧内容，代价是同一话题散落成多条历史记录，
   * 且旧回答彻底丢失。现在后端支持消息树，改后的提问会成为旧提问的
   * 兄弟版本，两者都留着，用 < n/m > 切换。
   *
   * @param {HTMLElement} row - 消息行
   * @param {string} text - 当前文本
   */
  function handleEdit(row, text) {
    if (state.isStreaming) return;
    enterEditMode(row, {
      onCancel: (original) => restoreUserMessage(row, original, handleEdit),
      onSubmit: (newText) => {
        const meta = getRowMessageMeta(row);
        truncateAfter(conversationContainer, row);
        setRowOriginalText(row, newText);
        restoreUserMessage(row, newText, handleEdit);
        callbacks.onTimelineUpdate();
        state.lastQuestion = newText;
        // parent_id 为 null 说明改的是第一条提问。JSON 里 null 表示
        // "不传"，无法表达"父节点是根层"，后端约定用 0 表达
        const parentId = meta.parentId === null ? 0 : meta.parentId;
        send({
          skipUserMessage: true,
          // parentId 为 undefined 说明这行还没跟后端消息绑定
          //（比如刚发出还没拿到 id），此时退回旧行为追加到末尾
          parentMessageId: parentId === undefined ? null : parentId,
          // 把被编辑的行传下去：done 事件回来时要用新的 user_message_id
          // 和版本数刷新它，否则切换器不会出现
          editedUserRow: row,
        });
      },
    });
  }

  /**
   * 追加一条用户消息到界面。
   * @param {string} content - 消息内容
   * @param {Object} [msg] - 后端消息对象，含 id / variant_count 等
   * @returns {HTMLElement}
   */
  function appendUserMessage(content, msg) {
    const sw = msg
      ? createVariantSwitch(msg, (dir) => switchVariant(msg, dir))
      : null;
    const row = createUserMessage(content, handleEdit, sw);
    if (msg) setRowMessageMeta(row, msg);
    conversationContainer.appendChild(row);
    return row;
  }

  /**
   * 编辑重发后，把用户消息行上的版本切换器补上。
   *
   * done 事件只带回助手消息的 variant_count，用户消息的拿不到，
   * 所以要单独查一次版本列表。查询失败不影响正文，静默降级。
   *
   * @param {HTMLElement} row - 用户消息行
   * @param {number} msgId - 该消息在后端的 id
   */
  async function refreshUserVariantSwitch(row, msgId) {
    if (!state.currentConversationId) return;
    let data;
    try {
      data = await api.getMessageVariants(state.currentConversationId, msgId);
    } catch {
      return;
    }

    const list = data.variants || [];
    if (list.length <= 1) return;

    const msg = {
      id: msgId,
      variant_count: list.length,
      variant_index: data.variant_index || 0,
    };
    setRowMessageMeta(row, { ...msg, parent_id: data.parent_id ?? null });

    const actions = $('.user-msg-actions', row);
    if (!actions) return;
    const old = $('.msg-variant-switch', actions);
    if (old) old.remove();

    const sw = createVariantSwitch(msg, (dir) => switchVariant(msg, dir));
    if (sw) actions.insertBefore(sw, actions.firstChild);
    refreshIcons();
  }

  /**
   * 切换到相邻的版本。
   * @param {Object} msg - 当前版本的消息对象
   * @param {number} dir - -1 上一个 / +1 下一个
   */
  async function switchVariant(msg, dir) {
    if (state.isStreaming || !state.currentConversationId) return;
    try {
      // 详情里只有 variant_index，拿不到兄弟版本的 id，得先取版本列表
      const data = await api.getMessageVariants(state.currentConversationId, msg.id);
      const list = data.variants || [];
      const target = list[(msg.variant_index || 0) + dir];
      if (!target) return;

      const detail = await api.switchVariant(state.currentConversationId, target.id);
      renderConversation(
        state.currentConversationId,
        detail.messages || [],
      );
    } catch (err) {
      console.error('切换版本失败:', err);
    }
  }

  /**
   * 重新生成最后一条回答。
   *
   * 同样不再新建对话：传 regenerate_of 让后端把新回答挂成
   * 旧回答的兄弟版本，旧回答仍然可以切回来看。
   *
   * @param {HTMLElement} [assistantRow] - 要重做的助手消息行，
   *                                       不传则取最后一条
   */
  function regenerate(assistantRow) {
    if (state.isStreaming) return;
    // $$ 返回的是 NodeList，转成数组才能用 indexOf
    const rows = Array.from($$('.message-row', conversationContainer));

    // 没指定就找最后一条助手消息
    let targetRow = assistantRow;
    if (!targetRow) {
      for (let i = rows.length - 1; i >= 0; i--) {
        if (rows[i].classList.contains('assistant-message')) { targetRow = rows[i]; break; }
      }
    }

    // 重新生成要基于它对应的提问，往前找最近的用户消息
    let lastUserRow = null;
    const startIdx = targetRow ? rows.indexOf(targetRow) : rows.length;
    for (let i = startIdx - 1; i >= 0; i--) {
      if (rows[i].classList.contains('user-message')) { lastUserRow = rows[i]; break; }
    }
    if (!lastUserRow) return;

    const meta = targetRow ? getRowMessageMeta(targetRow) : { id: null };

    truncateAfter(conversationContainer, lastUserRow);
    callbacks.onTimelineUpdate();
    state.lastQuestion = getRowOriginalText(lastUserRow);
    send({ skipUserMessage: true, regenerateOf: meta.id });
  }

  /**
   * 发送问题并消费流式回答。
   * @param {Object} [options] - 选项
   * @param {boolean} [options.skipUserMessage] - 跳过插入用户消息（重发/编辑场景）
   * @param {number} [options.parentMessageId] - 编辑重发时的父消息 id，
   *                                             改第一条时传 0
   * @param {number} [options.regenerateOf] - 重新生成时要重做的助手消息 id
   */
  async function send(options = {}) {
    const typed = options.skipUserMessage
      ? state.lastQuestion
      : textarea.value.trim();

    // 有引用卡片时，即使没打字也能发：引用本身就是内容
    if (!typed && !hasQuotes()) return;
    if (state.isStreaming) return;

    // 原文模式只读原文、不生成回答。此处打开阅读器，
    // 且不清空输入框——否则用户敲的字会凭空消失
    if (state.currentMode === 'original') {
      openReader();
      return;
    }

    // 引用只在首次发送时拼接：重发/编辑场景下 lastQuestion 里已经含着它了
    const question = options.skipUserMessage ? typed : composeWithQuotes(typed);
    if (!question) return;

    state.lastQuestion = question;

    showConversationView();
    // 编辑重发时用户行已经在界面上了，直接复用它，
    // done 回来时要往它身上刷新新的 id 与版本数
    let userRow = options.editedUserRow || null;
    if (!options.skipUserMessage) {
      userRow = appendUserMessage(question);
      textarea.value = '';
      clearQuotes();
      textarea.dispatchEvent(new Event('input'));
      callbacks.onTimelineUpdate();
    }

    textarea.disabled = true;
    setStreamingUI(true);

    const assistantRow = createAssistantMessage();
    const msgText = $('.msg-text', assistantRow);

    const thinkingPanel = createThinkingPanel();
    const searchPanel = createSearchRefsPanel();
    const loading = createLoadingIndicator();

    // 面板顺序(自上而下):阶段提示/解构卡片 → 搜索参考 → 思考 → 正文。
    // 思考放在"其他提示之后、正文之前",上下关系清楚:
    // 用户先看到系统在做什么(解构/检索),再看到思考过程,最后是成品。
    msgText.append(loading, searchPanel, thinkingPanel);
    conversationContainer.appendChild(assistantRow);
    refreshIcons();
    scrollToBottom(chatMessages, true);

    // 思考计时器
    const thinkStart = Date.now();
    const durationEl = $('.thinking-process-duration', thinkingPanel);
    const timer = setInterval(() => {
      durationEl.textContent = ((Date.now() - thinkStart) / 1000).toFixed(1) + 's';
    }, 100);

    let fullAnswer = '';
    let thinkingText = '';
    let sources = [];
    // done 事件带回的消息树字段，收到后才知道要不要渲染版本切换器
    let doneMeta = null;
    // 本轮用户消息的 id，编辑重发后要靠它刷新用户行的切换器
    let userMsgId = null;
    // 后端带回的各阶段耗时与引用覆盖率报告（问题 12 / 问题 7）
    let doneTimings = null;
    let refReport = null;
    let lastRenderTime = 0;
    let lastRenderedLen = 0;

    state.abortController = new AbortController();
    state.streamingConvId = state.currentConversationId;

    // 登记这一轮，切走再切回时靠它把节点接回界面
    activeStream = { userRow, assistantRow };

    try {
      const response = await api.postChat({
        question,
        conversation_id: state.currentConversationId,
        mode: state.currentMode,
        model: state.currentModel,
        thinking_effort: state.thinkingEffort,
        search_mode: state.searchMode,
        // 编辑重发：新提问与旧提问成为兄弟版本
        parent_message_id: options.parentMessageId ?? null,
        // 重新生成：新回答与旧回答成为兄弟版本
        regenerate_of: options.regenerateOf ?? null,
      }, state.abortController.signal);

      for await (const { event, data } of api.parseSSE(response.body)) {
        // 用户切到别的对话了。节点已经从界面上摘下来，但仍然照常更新——
        // 它们留在 activeStream 里，切回来时连同已生成的内容一起接回去。
        // 只有滚动这类会干扰当前视图的操作才需要跳过。
        const muted = isSwitchedAway();

        if (event === 'stage') {
          // 后端逐阶段汇报进度，首个反馈 0.04s 就到，
          // 而正文第一个字要等 20 秒，这段等待靠它撑住
          updateLoadingStage(loading, data);
          if (!muted) scrollToBottom(chatMessages);
        } else if (event === 'thinking' && data.content) {
          thinkingText += data.content;
          if (state.thinkingEffort !== 'off') {
            thinkingPanel.classList.add('visible');
            $('.thinking-process-body', thinkingPanel).textContent = thinkingText;
            // 思考内容持续增长时自动跟随滚动,但只在用户本就在底部附近时
            // 跟随——用户上划回看之前的内容时,绝不强制拉回底部。
            // 1) 面板展开时,面板内部滚动区同理:用户接近底部才滚到底;
            // 2) 消息区由 scrollToBottom 自带的 isNearBottom 保护。
            // 只有用户切走时才整体跳过。
            if (!muted && thinkingPanel.classList.contains('open')) {
              const body = $('.thinking-process-body', thinkingPanel);
              if (body && isNearBottom(body)) {
                body.scrollTop = body.scrollHeight;
              }
              scrollToBottom(chatMessages);
            }
          }
        } else if (event === 'thinking_done') {
          clearInterval(timer);
          if (state.thinkingEffort !== 'off') {
            $('.thinking-process-label', thinkingPanel).textContent = '已深度思考';
            const icon = $('.thinking-process-icon', thinkingPanel);
            if (icon) icon.style.animation = 'none';
          }
        } else if (event === 'search_result' && data.references) {
          if (state.searchMode) fillSearchRefs(searchPanel, data.references);
        } else if (event === 'token' && data.content) {
          fullAnswer += data.content;
          // 正文开始了，进度行收起来。但问题解构卡片留着：
          // 它说明了系统怎么理解这个问题，读完回答再回看仍有价值
          collapseLoadingStages(loading);
          // 节流:重绘只追加新增的完整段落(见 renderAnswer 增量逻辑),
          // 满 120ms 或积够 320 字才触发一次
          const now = Date.now();
          if (now - lastRenderTime > STREAM_RENDER.minIntervalMs
            || fullAnswer.length - lastRenderedLen > STREAM_RENDER.charThreshold) {
            renderAnswer(msgText, fullAnswer, [searchPanel, thinkingPanel]);
            lastRenderTime = now;
            lastRenderedLen = fullAnswer.length;
            if (!muted) scrollToBottom(chatMessages);
          }
        } else if (event === 'done') {
          clearInterval(timer);
          if (data.conversation_id) {
            // 顺序很重要：必须先用旧的 streamingConvId 判断用户有没有切走，
            // 再去更新它。反过来写的话，新对话（streamingConvId 初始为 null）
            // 会先被改成真实 id，紧接着 isSwitchedAway() 拿新 id 与仍是 null 的
            // currentConversationId 比较，误判成"已切走"，currentConversationId
            // 就永远补不上——下一条消息又带着 null 发出去，后端只好再建一个对话。
            const switchedAway = isSwitchedAway();
            // 新对话在后端首次落库后才拿到真正的 ID，
            // 这时 streamingConvId 还是 null，必须补上，
            // 否则 activeStream 的接回判断和思考内容落库都会失效
            if (state.streamingConvId === null) {
              state.streamingConvId = data.conversation_id;
            }
            // currentConversationId 只在用户仍停留在这一轮时才更新。
            // 他要是已经切到别的对话，改它会把界面强行拉回来
            if (!switchedAway) {
              state.currentConversationId = data.conversation_id;
            }
          }
          sources = data.sources || [];
          doneTimings = data.timings || null;
          refReport = data.ref_report || null;
          // 消息树字段：有了它们生成完就能直接渲染切换器，
          // 不必再拉一次对话详情
          doneMeta = {
            id: data.message_id ?? null,
            variant_count: data.variant_count || 1,
            variant_index: data.variant_index || 0,
          };
          // 用户消息的 id 也要回填到 DOM 上，
          // 否则紧接着再编辑这条提问时拿不到 parent_id
          if (data.user_message_id != null && userRow) {
            userRow.dataset.msgId = String(data.user_message_id);
            userMsgId = data.user_message_id;
          }
          // 思考内容与解构卡片不再存 localStorage：
          // 后端 done 时已随消息落库（thinking_content / stage_detail），
          // 刷新页面后由 renderConversation 从后端消息对象还原。
        } else if (event === 'error') {
          throw new Error(data.detail || '未知错误');
        }
      }

      clearInterval(timer);
      // 缓存命中这类没有 token 事件的场景，收尾时才走到这里
      collapseLoadingStages(loading);

      // 这些都只改这一轮自己的节点，即使用户切走了也要补完：
      // 节点会随 activeStream 一起被接回界面，不补的话正文会停在
      // 最后一次节流渲染的位置，来源卡片和操作按钮也不会出现
      if (fullAnswer) {
        // force:流结束时把最后一截没渲染的尾段一次补全
        renderAnswer(msgText, fullAnswer, [searchPanel, thinkingPanel], true);
      }
      if (sources.length) appendSources(assistantRow, sources, refReport);
      showTimings(assistantRow, doneTimings);

      if (doneMeta) setRowMessageMeta(assistantRow, doneMeta);
      // 重新生成过的回答会有多个版本，这时才需要切换器
      const sw = doneMeta
        ? createVariantSwitch(doneMeta, (dir) => switchVariant(doneMeta, dir))
        : null;
      appendMessageActions(
        assistantRow, fullAnswer, () => regenerate(assistantRow), sw,
      );
      // 编辑重发会让这条提问多出一个版本，但 done 事件只带回助手消息的
      // 版本数，用户消息的得单独查一次才能把切换器补上
      if (options.editedUserRow && userMsgId) {
        refreshUserVariantSwitch(options.editedUserRow, userMsgId);
      }
      // 滚动只在用户正看着这个对话时才做，否则会打断他在别处的浏览
      if (!isSwitchedAway()) scrollToBottom(chatMessages);

      callbacks.onHistoryRefresh();

    } catch (err) {
      clearInterval(timer);
      if (loading.parentNode) loading.remove();

      if (err.name === 'AbortError') {
        // 用户主动停止：保留已生成的部分
        if (fullAnswer) {
          renderAnswer(msgText, fullAnswer, [searchPanel, thinkingPanel], true);
          appendMessageActions(assistantRow, fullAnswer, () => regenerate(assistantRow));
        } else {
          msgText.innerHTML = '';
          const tip = document.createElement('p');
          tip.className = 'msg-stopped-hint';
          tip.textContent = '已停止生成';
          msgText.appendChild(tip);
        }
      } else {
        console.error('Chat error:', err);
        msgText.innerHTML = '';
        const errEl = document.createElement('p');
        errEl.className = 'msg-error';
        errEl.textContent = '出错: ' + err.message;
        msgText.appendChild(errEl);
      }
    } finally {
      // 流结束时用户还在别的对话上：这一轮的节点此刻不在 DOM 里，
      // 而 activeStream 马上要被清掉，节点就没人持有了。
      // 好在内容已经落库，下次打开这个对话会从后端正常渲染出来，
      // 所以这里只需要把引用释放掉。
      const finishedConvId = state.streamingConvId;
      activeStream = null;

      state.abortController = null;
      state.streamingConvId = null;
      setStreamingUI(false);
      callbacks.onTimelineUpdate();

      // 用户切走时不要抢焦点，否则会把他正在别处的输入打断
      if (finishedConvId === null || finishedConvId === state.currentConversationId) {
        textarea.focus();
      }
    }
  }

  /**
   * 中断当前生成。
   */
  function stop() {
    if (state.abortController) {
      state.abortController.abort();
      state.abortController = null;
    }
  }

  /**
   * 清空界面，开始一个新对话。
   */
  function newChat() {
    state.currentConversationId = null;
    state.lastQuestion = '';
    welcomeContainer.style.display = 'flex';
    conversationContainer.innerHTML = '';
    conversationContainer.style.display = 'none';
    textarea.value = '';
    textarea.dispatchEvent(new Event('input'));
    callbacks.onTimelineUpdate();
  }

  /**
   * 加载一个历史对话并渲染。
   * @param {string} convId - 对话 ID
   * @param {Array} messages - 消息列表（含 thinking_content / stage_detail）
   */
  function renderConversation(convId, messages) {
    state.currentConversationId = convId;
    state.lastQuestion = '';
    showConversationView();
    conversationContainer.innerHTML = '';

    // 切回正在生成的那个对话：后端还没落库这一轮，
    // 拉回来的 messages 里没有正在流式的回答，
    // 所以要把留在 activeStream 里的节点接回去
    const resuming = activeStream && state.isStreaming
      && state.streamingConvId === convId;

    messages.forEach((msg) => {
      // 正在流式的这一轮已经有现成节点了，跳过后端返回的同一条，
      // 否则用户消息会重复出现两次
      if (resuming && activeStream.userRow
        && String(msg.id) === activeStream.userRow.dataset.msgId) {
        return;
      }

      if (msg.role === 'user') {
        // 切换器在 appendUserMessage 内部按 msg 建，这里不重复建
        appendUserMessage(msg.content, msg);
      } else if (msg.role === 'assistant') {
        // 后端只返回激活分支上的消息，variant_count > 1 说明这一层
        // 还有别的版本，要给用户一个切过去的入口
        const sw = createVariantSwitch(msg, (dir) => switchVariant(msg, dir));
        const row = createAssistantMessage();
        setRowMessageMeta(row, msg);
        const msgText = $('.msg-text', row);

        // 思考过程与解构卡片已随消息落库（后端字段），
        // 直接还原，不再依赖 localStorage 的序号映射。
        // 面板顺序与流式渲染一致:解构卡片在前,思考在后,正文最后。
        const panels = [];

        // 还原问题解构卡片。SSE 期间它挂在 .thinking-steps 容器里，
        // 这里用同样的容器包一层，位置和样式才与生成时一致
        const detail = msg.stage_detail;
        if (detail) {
          const card = createStageDetail(detail);
          if (card) {
            const box = document.createElement('div');
            box.className = 'thinking-steps';
            box.appendChild(card);
            panels.push(box);
          }
        }
        if (msg.thinking_content) {
          panels.push(createThinkingPanel(msg.thinking_content, true));
        }

        renderAnswer(msgText, msg.content || '', panels);
        conversationContainer.appendChild(row);

        if (msg.sources && msg.sources.length) appendSources(row, msg.sources);
        if (msg.content) {
          appendMessageActions(row, msg.content, () => regenerate(row), sw);
        }
      }
    });

    if (resuming) {
      // 节点还带着已生成的正文与思考内容，直接挂回末尾即可续看
      if (activeStream.userRow) conversationContainer.appendChild(activeStream.userRow);
      conversationContainer.appendChild(activeStream.assistantRow);
    }

    refreshIcons();
    callbacks.onTimelineUpdate();
    scrollToBottom(chatMessages, true);
  }

  return { send, stop, newChat, renderConversation, setStreamingUI, regenerate };
}
