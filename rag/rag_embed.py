import os
import time
import logging
import argparse
from typing import List, Dict, Optional
from dotenv import load_dotenv
from openai import OpenAI

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(module)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 加载配置 (override=True确保优先读取.env文件)
load_dotenv(override=True)

# 默认配置
EMBEDDING_MODEL = os.getenv("EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B")
EMBEDDING_DIM = 1024
API_BASE_URL = os.getenv("EMBED_API_BASE_URL", "https://api2.aigcbest.top/v1")
API_KEY = os.getenv("EMBED_API_KEY", "")
MAX_TEXT_LENGTH = 8000
MAX_BATCH_SIZE = 100
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 60.0
MAX_RETRIES = 3

# 异常体系
class EmbeddingError(Exception): pass
class InputValidationError(EmbeddingError): pass
class APIRequestError(EmbeddingError): pass
class APITimeoutError(EmbeddingError): pass
class APIResponseError(EmbeddingError): pass
class DimensionMismatchError(EmbeddingError): pass

class QwenEmbedder:
    """Qwen3-Embedding-0.6B 向量化模块"""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or API_KEY
        self.base_url = base_url or API_BASE_URL
        
        if not self.api_key:
            raise InputValidationError("API_KEY 不能为空，请在 .env 文件中配置 EMBED_API_KEY")
            
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=READ_TIMEOUT,
            max_retries=MAX_RETRIES
        )
        logger.debug(f"已初始化 QwenEmbedder, 模型: {EMBEDDING_MODEL}")

    def _validate_input(self, text: str) -> str:
        """输入验证"""
        if not isinstance(text, str):
            raise InputValidationError(f"输入类型错误，期望 str，实际为 {type(text)}")
            
        text = text.strip()
        if not text:
            raise InputValidationError("输入文本不能为空")
            
        # 清理终端传递过来的无效代理对（surrogate）字符
        # 当终端编码与 Python 内部 UTF-8 编码不一致时，用户输入可能包含 surrogate 字符
        # 这会导致后续 JSON 序列化时抛出 UnicodeEncodeError
        try:
            text.encode('utf-8')
        except UnicodeEncodeError:
            logger.warning("检测到无效 surrogate 字符，正在进行清理...")
            text = text.encode('utf-8', 'surrogatepass').decode('utf-8', 'replace')
            
        if len(text) > MAX_TEXT_LENGTH:
            logger.warning(f"输入文本长度({len(text)})超过最大限制({MAX_TEXT_LENGTH})，将进行截断")
            text = text[:MAX_TEXT_LENGTH]
            
        return text

    def _call_api(self, texts: List[str]) -> List[List[float]]:
        """调用 API 进行向量化"""
        start_time = time.time()
        
        try:
            resp = self.client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
            )
            
            elapsed = time.time() - start_time
            logger.info(f"成功调用 API, 批次大小: {len(texts)}, 耗时: {elapsed:.2f}s")
            
            embeddings = [item.embedding for item in resp.data]
            
            # 维度校验
            for i, emb in enumerate(embeddings):
                if len(emb) != EMBEDDING_DIM:
                    raise DimensionMismatchError(f"第 {i} 条向量维度不匹配: 期望 {EMBEDDING_DIM}, 实际 {len(emb)}")
                    
            return embeddings
            
        except Exception as e:
            logger.error(f"API 调用失败: {type(e).__name__} - {str(e)}")
            raise APIRequestError(f"API 调用失败: {str(e)}") from e

    def embed_single(self, text: str) -> List[float]:
        """单文本向量化"""
        valid_text = self._validate_input(text)
        embeddings = self._call_api([valid_text])
        return embeddings[0]

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """批量文本向量化"""
        if not texts:
            return []
            
        valid_texts = []
        for i, text in enumerate(texts):
            try:
                valid_texts.append(self._validate_input(text))
            except InputValidationError as e:
                logger.warning(f"第 {i} 条文本验证失败: {str(e)}，已跳过")
                
        all_embeddings = []
        
        # 分批处理
        for i in range(0, len(valid_texts), MAX_BATCH_SIZE):
            batch = valid_texts[i : i + MAX_BATCH_SIZE]
            logger.info(f"正在处理第 {i+1} 到 {i+len(batch)} 条数据...")
            batch_embeddings = self._call_api(batch)
            all_embeddings.extend(batch_embeddings)
            
        return all_embeddings

    def embed_file(self, file_path: str) -> Dict[int, List[float]]:
        """从文件读取并向量化"""
        if not os.path.exists(file_path):
            raise InputValidationError(f"文件不存在: {file_path}")
            
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # 简单按双换行分块
        chunks = [c.strip() for c in content.split('\n\n') if c.strip() and len(c.strip()) >= 10]
        logger.info(f"从文件 {file_path} 中提取出 {len(chunks)} 个有效文本块")
        
        embeddings = self.embed_batch(chunks)
        
        # 返回映射
        return {i: emb for i, emb in enumerate(embeddings)}
        
    def get_embedding_dim(self) -> int:
        """获取模型输出的向量维度"""
        return EMBEDDING_DIM

    def health_check(self) -> bool:
        """API连通性检查"""
        logger.info("开始健康检查...")
        try:
            emb = self.embed_single("健康检查测试")
            if len(emb) == EMBEDDING_DIM:
                logger.info("健康检查通过，API 连接正常。")
                return True
        except Exception as e:
            logger.error(f"健康检查失败: {str(e)}")
            
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="文档向量化模块")
    parser.add_argument("--input", type=str, help="要向量化的单条文本")
    parser.add_argument("--file", type=str, help="要向量化的文件路径")
    parser.add_argument("--health", action="store_true", help="执行健康检查")
    parser.add_argument("--debug", action="store_true", help="开启调试日志")
    
    args = parser.parse_args()
    
    if args.debug:
        logger.setLevel(logging.DEBUG)
        
    embedder = QwenEmbedder()
    
    if args.health:
        embedder.health_check()
    elif args.input:
        res = embedder.embed_single(args.input)
        print(f"向量化成功，维度: {len(res)}，前5个值: {res[:5]}")
    elif args.file:
        res = embedder.embed_file(args.file)
        print(f"文件处理完成，共生成 {len(res)} 条向量")
    else:
        parser.print_help()
