from datetime import datetime
from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator


# ---------- Tag ----------
class TagBase(BaseModel):
    name: str
    color: Optional[str] = "#1890ff"
    description: Optional[str] = None


class TagCreate(TagBase):
    pass


class TagResponse(TagBase):
    id: int

    class Config:
        from_attributes = True


# ---------- Paper ----------
class PaperBase(BaseModel):
    title: Optional[str] = None
    authors: Optional[str] = None
    year: Optional[int] = None
    journal: Optional[str] = None
    abstract: Optional[str] = None
    doi: Optional[str] = None
    pages: Optional[int] = None
    status: Optional[str] = "unread"
    source: Optional[str] = "local"
    processed: Optional[str] = "pending"


class PaperCreate(PaperBase):
    file_path: str
    filename: str
    metadata_json: Optional[Dict[str, Any]] = None


class PaperUpdate(PaperBase):
    metadata_json: Optional[Dict[str, Any]] = None


class PaperListItem(BaseModel):
    id: int
    title: Optional[str]
    authors: Optional[str]
    year: Optional[int]
    journal: Optional[str]
    status: str
    processed: str
    filename: str
    last_read_page: Optional[int]
    created_at: datetime
    tags: List[TagResponse] = []

    class Config:
        from_attributes = True


class PaperDetail(PaperListItem):
    abstract: Optional[str]
    doi: Optional[str]
    pages: Optional[int]
    file_path: str
    source: str
    metadata_json: Optional[Dict[str, Any]]
    last_read_page: Optional[int]
    updated_at: datetime

    class Config:
        from_attributes = True


class PaperListResponse(BaseModel):
    total: int
    items: List[PaperListItem]


class PaperStatsResponse(BaseModel):
    total: int
    by_year: Dict[str, int]
    by_status: Dict[str, int]
    by_tag: Dict[str, int]
    top_authors: List[Dict[str, Any]]
    citation_graph: Dict[str, Any]


class PaperBatchDelete(BaseModel):
    ids: List[int]


class PaperBatchStatus(BaseModel):
    ids: List[int]
    status: str


class PaperBatchTags(BaseModel):
    ids: List[int]
    tag_names: List[str]
    action: str = "add"  # add / remove


# ---------- Chunk ----------
class ChunkResponse(BaseModel):
    id: int
    paper_id: int
    content: str
    page_number: Optional[int]
    chunk_index: int
    section_title: Optional[str]
    chunk_type: str

    class Config:
        from_attributes = True


# ---------- Search ----------
class SearchRequest(BaseModel):
    query: str
    top_k: Optional[int] = 10
    filters: Optional[Dict[str, Any]] = Field(default_factory=dict)
    use_keyword: Optional[bool] = True
    use_semantic: Optional[bool] = True


class SearchResult(BaseModel):
    paper_id: int
    title: Optional[str]
    authors: Optional[str]
    year: Optional[int]
    content: str
    page_number: Optional[int]
    chunk_type: str
    score: float
    source: str  # semantic / keyword / hybrid


class SearchResponse(BaseModel):
    query: str
    results: List[SearchResult]


# ---------- Chat ----------
class ChatMessage(BaseModel):
    role: str
    content: str
    citations: Optional[List[Dict[str, Any]]] = None


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[int] = None
    paper_id: Optional[int] = None
    stream: Optional[bool] = True
    enable_web_search: Optional[bool] = False
    skill: Optional[str] = None  # translator / proofreader / method_comparator / outline_generator / data_analyst


class ChatStreamChunk(BaseModel):
    delta: str
    finished: bool = False
    citations: Optional[List[Dict[str, Any]]] = None
    image_analysis: Optional[bool] = False


class DeepReviewRequest(BaseModel):
    """深度综述长任务请求（Phase F F2）：topic 必填，conversation_id 可选（缺省自动建会话）。"""
    topic: str
    conversation_id: Optional[int] = None


class ImageAnalysisRequest(BaseModel):
    question: Optional[str] = "请描述这张图片的内容，并解释其在学术论文中可能的含义。"


# ---------- Conversation ----------
class ConversationResponse(BaseModel):
    id: int
    title: Optional[str]
    summary: Optional[str]
    message_count: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------- Thesis ----------
class ThesisChapter(BaseModel):
    title: str
    level: int
    start_paragraph: int
    end_paragraph: int


class ThesisCitationResponse(BaseModel):
    id: int
    thesis_id: int
    paper_id: Optional[int]
    chapter_index: Optional[int]
    section_index: Optional[int]
    context: Optional[str]
    citation_text: Optional[str]
    detected_auto: bool

    class Config:
        from_attributes = True


class ThesisFileResponse(BaseModel):
    id: int
    title: Optional[str]
    filename: str
    chapter_structure: List[ThesisChapter] = []
    word_count: Optional[int]
    metadata_json: Optional[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChapterCitationMapItem(BaseModel):
    chapter_index: int
    chapter_title: str
    level: int
    paper_ids: List[int]
    paper_titles: Dict[int, Optional[str]]
    citation_count: int


class ThesisCitationMapResponse(BaseModel):
    thesis_id: int
    total_citations: int
    matched_citations: int
    chapters: List[ChapterCitationMapItem]


class ThesisCitationUpdate(BaseModel):
    paper_id: Optional[int] = None


class PaperAnnotationBase(BaseModel):
    page_number: int
    selected_text: str
    note: Optional[str] = None
    color: Optional[str] = "yellow"


class PaperAnnotationCreate(PaperAnnotationBase):
    pass


class PaperAnnotationResponse(PaperAnnotationBase):
    id: int
    paper_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class ThesisFileListResponse(BaseModel):
    total: int
    items: List[ThesisFileResponse]


class ThesisAnalyzeRequest(BaseModel):
    chapter_index: Optional[int] = None


class ThesisSuggestRequest(BaseModel):
    paragraph: str = Field(min_length=1, max_length=20000)

    @field_validator("paragraph", mode="before")
    @classmethod
    def trim_paragraph(cls, value):
        """请求入口统一去除首尾空白，纯空白交给长度约束拒绝。"""
        return value.strip() if isinstance(value, str) else value


class ThesisAnalyzeResponse(BaseModel):
    thesis_id: int
    chapter_title: Optional[str]
    suggestions: str
    citations: List[ThesisCitationResponse] = []


# ===== Phase G G2：引用图谱响应模型（本区由 G2 代理追加，合并冲突时保留本区整体）=====
class CitationGraphNode(BaseModel):
    """引用图谱节点：文献 id / 标题 / 年份。"""

    id: int
    title: Optional[str] = None
    year: Optional[int] = None


class CitationGraphEdge(BaseModel):
    """引用图谱边：citing 引用 cited（均对应 papers.id）。"""

    citing: int
    cited: int


class CitationGraphResponse(BaseModel):
    """GET /api/papers/{id}/citation-graph 响应：以该文献为中心的 1 跳子图。"""

    nodes: List[CitationGraphNode] = []
    edges: List[CitationGraphEdge] = []


class BenchmarkV2ReadinessResponse(BaseModel):
    """只公开聚合计数的 Benchmark v2 就绪度响应。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["PASS", "WAIT", "UNAVAILABLE"]
    ready: StrictBool
    minimum_new_papers: Literal[12]
    missing_new_papers: Optional[StrictInt] = Field(default=None, ge=0)
    physical_pdf_files: Optional[StrictInt] = Field(default=None, ge=0)
    unique_pdf_contents: Optional[StrictInt] = Field(default=None, ge=0)
    duplicate_pdf_files: Optional[StrictInt] = Field(default=None, ge=0)
    covered_unique_contents: Optional[StrictInt] = Field(default=None, ge=0)
    eligible_imported_papers: Optional[StrictInt] = Field(default=None, ge=0)
    unimported_unique_contents: Optional[StrictInt] = Field(default=None, ge=0)
    error_code: Optional[Literal["benchmark_data_unavailable"]] = None
