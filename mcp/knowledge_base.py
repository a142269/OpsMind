"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后存入 ChromaDB（自动生成 Embedding）
  2. 语义检索：根据 query 从知识库中检索最相关的文档片段
  3. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    COLLECTION_NAME = "knowledge_base"

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
    ):
        # 优先连接独立 ChromaDB 服务（服务端内置 embedding 模型，客户端无需下载）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 使用服务端时不传 embedding_function，让服务端处理
        # 本地模式时也不传，使用 ChromaDB 默认的（会触发模型下载）
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={"description": "OpsMind RAG 知识库"},
        )

        # 如果知识库为空，导入默认文档
        if self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片 500 字）。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title   = doc.get("title", "")
            content = doc.get("content", "")
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({"title": title, "chunk_index": i, "total_chunks": len(chunks)})

        if ids:
            # ID 由文档内容稳定生成；upsert 使重复导入幂等，同时允许同 ID 内容更新。
            # ChromaDB 会自动生成或更新 Embedding。
            self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"知识库写入 {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        语义检索：根据 query 返回最相关的文档片段。

        ChromaDB 内部自动将 query 转为向量，与存储的文档向量做余弦相似度匹配。
        """
        results = self._collection.query(
            query_texts=[query],
            n_results=top_k,
        )

        items = []
        if results["documents"] and results["documents"][0]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                items.append({
                    "title":    meta.get("title", ""),
                    "content":  doc,
                    "score":    round(1.0 - dist, 4),  # ChromaDB 返回距离，转为相似度
                    "chunk":    meta.get("chunk_index", 0),
                })

        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        """导入 OpsMind 企业 IT 服务台的默认演示制度与 SOP。"""
        default_docs = [
            {
                "title": "OpsMind 演示制度：服务状态与工单",
                "content": (
                    "本文仅为 OpsMind 演示制度，不表示已接入真实企业的状态监控或工单系统。"
                    "演示服务状态分为正常、性能下降、部分不可用、重大中断和维护中；状态页内容只作字段解释，不能当作实时监测结果。"
                    "演示工单状态包括待分派、处理中、等待用户补充、已解决和已关闭。"
                    "咨询进度时可说明需要工单号、所属系统和联系人，但不得虚构受理人、处理进度或恢复时间。"
                    "演示 SOP 要求优先复述用户需求，再给出查询路径或需要补充的信息；没有真实查询结果时应明确说明。"
                ),
            },
            {
                "title": "OpsMind 演示服务目录：办公时间与咨询渠道",
                "content": (
                    "以下内容仅为 OpsMind 演示环境配置，不代表任何真实企业的服务承诺。"
                    "演示人工服务台办公时间为工作日 09:00-18:00，法定节假日除外；自助知识检索与请求草稿整理可全天使用。"
                    "演示咨询渠道包括本对话窗口、企业内网 IT 服务台门户和服务台邮箱三种方式。"
                    "当前演示未配置真实门户 URL 或邮箱地址，实际部署时应由企业管理员配置，不能编造联系方式。"
                    "安全事件和生产重大中断应按企业既有紧急值班渠道升级，不受演示办公时间限制。"
                ),
            },
            {
                "title": "OpsMind 演示 SOP：故障诊断与事件升级",
                "content": (
                    "本文仅为 OpsMind 演示故障诊断 SOP，不代表能够探测真实企业服务。"
                    "遇到应用报错，应收集发生时间、应用名称、错误码、影响范围和已尝试操作，不要索取密码或完整敏感日志。"
                    "遇到性能下降，应区分单个用户、单个地点与多用户共同受影响，并记录响应耗时和高峰时段。"
                    "多部门同时不可用可按重大中断演示流程升级；单用户功能异常先进行基础排查。"
                    "回复只能给出诊断步骤和升级建议，不能声称已重启服务、已定位根因或已恢复。"
                ),
            },
            {
                "title": "OpsMind 演示制度：账号访问",
                "content": (
                    "本文仅为 OpsMind 演示账号访问制度，不连接真实身份提供商。"
                    "登录失败时先核对账号状态、认证方式和错误提示，再建议使用企业认可的自助重置流程。"
                    "忘记密码、更换绑定手机号、设备不受信和验证码未收到属于账号访问问题，应分别记录，不要反复要求用户提供密码。"
                    "账号停用、离职归档和限时账号需按演示身份生命周期流程由授权人员处理。"
                    "服务台只能指导操作或说明所需材料，不能声称已经完成密码重置、账号解锁或换绑。"
                ),
            },
            {
                "title": "OpsMind 演示制度：权限申请",
                "content": (
                    "本文仅为 OpsMind 演示权限制度，不代表已接入真实审批平台。"
                    "权限申请应说明目标系统、资源范围、角色、使用目的和期望期限，并遵循最小权限原则。"
                    "普通查看权限按演示流程由直属主管审批；敏感数据、导出或管理员权限还需资源负责人与安全角色复核。"
                    "临时权限应设置到期时间，岗位变化后应重新核对保留范围。"
                    "服务台可检查申请材料是否完整并解释退回原因，但不能承诺审批通过或声称已经授权。"
                ),
            },
            {
                "title": "OpsMind 演示 SOP：安全事件响应",
                "content": (
                    "本文仅为 OpsMind 演示安全事件 SOP，不提供真实安全运营中心接入。"
                    "疑似账号盗用、异常登录、钓鱼链接、恶意软件或敏感数据异常访问都应按安全事件处理。"
                    "优先建议停止可疑操作、保留原始证据并通过可信渠道报告；不要点击可疑链接，不要转发恶意附件，不要泄露验证码。"
                    "记录发现时间、涉及账号或设备、异常现象和已采取措施，避免在对话中粘贴机密文件内容。"
                    "紧急事件应升级人工安全角色；回复不得声称已完成封禁、取证或威胁清除。"
                ),
            },
            {
                "title": "OpsMind 演示 SOP：变更发布与回滚",
                "content": (
                    "本文仅为 OpsMind 演示变更制度，不连接真实 CI/CD、配置管理或生产环境。"
                    "变更申请应包含影响范围、实施步骤、验证方法、风险、维护窗口和可执行的回滚方案。"
                    "演示流程要求审批通过后才能进入发布窗口，高风险变更还需安排值班人员和业务验证人。"
                    "出现核心功能不可用、错误率显著上升或数据异常时，应按预案评估停止发布或回滚，并保留变更记录。"
                    "服务台只能解释流程和检查材料，不能声称已审批、已发布或已回滚，也不能编造生产执行结果。"
                ),
            },
        ]
        self.add_documents(default_docs)
        logger.info(f"已导入默认知识库: {len(default_docs)} 篇文档")
