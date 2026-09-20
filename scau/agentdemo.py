from pymilvus.client.types import MetricType
from pymilvus import MilvusClient

# =========================
# 基本配置
# =========================
MILVUS_URI = "http://localhost:19530"  # Milvus 服务的连接地址
DB_NAME = "rag_tutorial"    # 自定义数据库名称
COLLECTION_NAME = "docs"    # 向量集合名称（类似于传统数据库的表）
KNOWLEDGE_FILE = "../knowledge.txt"  # 本地知识库文件路径

# BGE-M3 在 SiliconFlow / Milvus 文档中都是 1024 维
EMBED_MODEL_NAME = "qwen3-vl-embedding"   # 嵌入模型名称
EMBED_DIM = 1024   # BGE-M3 模型输出的向量维度固定为 1024

#初始化Milvus客户端
client = MilvusClient(MILVUS_URI)

# 查询已有的数据库，如果不存在指定名的数据库，则进行创建
existed_databases = client.list_databases()
if DB_NAME not in existed_databases:
    client.create_database(db_name=DB_NAME)

# 切换到指定的数据库
client.use_database(db_name=DB_NAME)