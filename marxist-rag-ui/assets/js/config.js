/**
 * config.js —— 全局常量与运行时配置
 *
 * 集中存放散落在各处的魔法数字和 localStorage 键名，
 * 避免同一个字符串在多个文件里手写导致拼写漂移。
 */

/** 后端 API 前缀。前后端同源部署，用相对路径即可。 */
export const API_BASE = '/api';

/** localStorage 键名清单 */
export const STORAGE_KEYS = {
  /** 思考强度（"off" / "high" / "max"，参考 DSH 推理等级） */
  thinkingEffort: 'thinking_effort',
  /** 联网搜索开关（"true" / "false"） */
  searchMode: 'search_mode',
  /** 界面主题（"light" / "dark" / "auto"） */
  theme: 'theme',
  /** 分组列表 [{ id, name }] */
  folders: 'folders',
  /** 对话 → 分组的映射 { convId: folderId } */
  convFolders: 'conv_folders',
  /** 思考内容前缀，实际键为 thinking_<convId> */
  thinkingPrefix: 'thinking_',
  /** 问题解构结果前缀，实际键为 stage_detail_<convId> */
  stageDetailPrefix: 'stage_detail_',
  /** 阅读器上次读到的位置 { source, seq } */
  readerLast: 'reader_last',
};

/** 流式渲染节流参数 */
export const STREAM_RENDER = {
  /** 两次 Markdown 重绘的最小间隔（毫秒）。
   *  增量渲染落地后可以放宽间隔：重绘只追加新段落而非全文 */
  minIntervalMs: 120,
  /** 累积多少个字符就强制重绘一次（应对高速流） */
  charThreshold: 320,
};

/** 三种问答模式的说明文案，显示在输入框上方 */
export const MODE_DESCRIPTIONS = {
  general: '基于马克思主义理论回答一般问题',
  methodology: '运用马克思主义哲学方法论分析问题',
  original: '打开原文阅读器，直接阅读马克思主义经典著作',
};

/** 阅读器定位高亮的持续时间（毫秒） */
export const READER_HIGHLIGHT_MS = 4000;

/** 设置项分类的中文名 */
export const CATEGORY_LABELS = {
  general: '通用',
  api: 'API 配置',
  search: '检索',
  about: '关于',
};

/** 历史对话列表一次拉取的条数 */
export const HISTORY_LIMIT = 50;

/** 单个对话最多保留多少条思考记录，超出后裁剪 */
export const THINKING_MAX_ENTRIES = 100;

/** 裁剪时保留最近的条数 */
export const THINKING_KEEP_ENTRIES = 50;

/** 输入框最大高度（像素），超过后内部滚动 */
export const TEXTAREA_MAX_HEIGHT = 200;

/** 自动滚动的判定阈值：距底部小于此值才跟随滚动 */
export const SCROLL_BOTTOM_THRESHOLD = 150;

/** 移动端断点，与 app.css 中的 @media 保持一致 */
export const MOBILE_BREAKPOINT = 768;

/** 后端无响应时的兜底模型，避免选择器一直显示"加载中" */
export const FALLBACK_MODEL = { id: 'deepseek-chat', name: 'DeepSeek-V3', provider: 'deepseek' };
