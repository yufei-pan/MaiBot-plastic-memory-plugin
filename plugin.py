"""塑料内存条 (Plastic Memory) 插件。

为麦麦提供可自行管理的便利贴笔记：
- 全局便利贴（my_memory.md）与按聊天流隔离的便利贴（chat_notes/<stream_id>.md）；
- 在每次 planner / 时机判断 / replyer 请求的系统提示词之后注入笔记内容与剩余空间；
- 暴露 append_note / rewrite_note / compact_notes 三个工具供麦麦自行维护笔记；
- 当笔记超过 size_limit（字符数）时，自动或主动触发基于 LLM 的压缩重写。
"""

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any, Literal, Optional, Set

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType


# 默认注入模板，可在 config.toml 中覆盖。说明性文字默认置于【全局便利贴】之前便于模型缓存。
# 占位符：{nickname}、{note}、{used}、{free}、{size_limit}、{stream_section}
DEFAULT_INJECTION_TEMPLATE = """以下便利贴是你（{nickname}）自己写给自己的备忘。可用 append_note、rewrite_note、compact_notes 维护（参数 scope 默认 global 记全局，stream 记本聊天流）。

【全局便利贴】
{note}
（{used}/{size_limit}）{stream_section}"""

# 当全局笔记为空时，用于填充 {note} 占位符的提示文本。
DEFAULT_EMPTY_NOTE_HINT = "（空）"

# 本聊天流便利贴注入子模板（由代码渲染后填入 injection_template 的 {stream_section}）。
# 可在 config.toml 的 stream_injection_section 中覆盖。
DEFAULT_STREAM_INJECTION_SECTION = """
【本聊天流便利贴】
{stream_note}
（{stream_used}/{stream_size_limit}）"""

# 当本聊天流笔记为空时，用于填充 {stream_note} 占位符的提示文本。
DEFAULT_EMPTY_STREAM_NOTE_HINT = "（空）"

SCOPE_TOOL_PARAM = ToolParameterInfo(
    name="scope",
    param_type=ToolParamType.STRING,
    description='写入范围："global"（默认，全局便利贴）或 "stream"（仅当前聊天流便利贴）。',
    required=False,
)

# HookHandler 处理器的默认超时时间（毫秒）。可通过 config.toml 的 hook_timeout_ms 覆盖。
DEFAULT_HOOK_TIMEOUT_MS = 60000

# compact_max_tokens = 0 时，自动 max_tokens = size_limit * 此倍数（推理模型会消耗大量 reasoning token）。
AUTO_COMPACT_MAX_TOKENS_MULTIPLIER = 8

# 单次压缩 LLM 调用链：首次请求 + 最多 MAX_COMPACT_LENGTH_RETRIES 次翻倍重试 = 最多 4 次调用。
MAX_COMPACT_LENGTH_RETRIES = 3

# 若返回笔记字符数低于 size_limit 的这一比例，视为可能被长度截断并触发翻倍重试。
COMPACT_LENGTH_RETRY_RATIO = 0.8

# Host 若将来暴露 finish_reason，这些值表示输出因长度/ token 上限被截断。
_LENGTH_FINISH_REASONS = frozenset(
    {
        "length",
        "max_tokens",
        "max_output_tokens",
        "content_filter_length",
    }
)

# 默认的压缩提示词模板，可在 config.toml 中覆盖。
# 占位符：{nickname}、{personality}、{reply_style}、{note_scope}、{size_limit}、{used}、{note}
DEFAULT_COMPACT_PROMPT_TEMPLATE = """你是{nickname}。
你的人格设定：{personality}
你的表达风格：{reply_style}

下面这份{note_scope}便利贴笔记是你写给自己的备忘，但它太长了：当前 {used} 字符，必须压缩到 {size_limit} 字符以内。
请你以{nickname}的身份重写这份笔记，在尽量保留关键信息、待办事项与重要事实的前提下让它更精炼。
只输出压缩后的笔记正文本身，不要输出任何解释、前言或额外说明。

当前笔记内容：
{note}"""


def _render(template: str, **values: Any) -> str:
    """使用简单的占位符替换渲染模板。

    采用 ``str.replace`` 而非 ``str.format``，以避免笔记/人格文本中出现的
    花括号导致 ``KeyError`` 或格式化异常。

    Args:
        template: 含 ``{key}`` 占位符的模板字符串。
        **values: 占位符到实际值的映射。

    Returns:
        str: 渲染后的字符串。
    """
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered


def _resolve_stream_id(kwargs: dict[str, Any]) -> str:
    """从工具或 Hook 上下文中解析当前聊天流 ID。"""
    for key in ("stream_id", "session_id", "chat_id"):
        value = kwargs.get(key)
        if value:
            return str(value).strip()
    return ""


def _safe_stream_filename(stream_id: str) -> str:
    """将 stream_id 转为安全的文件名（不含路径分隔符）。"""
    sanitized = re.sub(r'[<>:"/\\|?\x00]', "_", stream_id.strip())
    sanitized = sanitized.strip(". ")
    if sanitized:
        return sanitized
    return hashlib.sha256(stream_id.encode("utf-8")).hexdigest()[:16]


def _normalize_scope(scope: str) -> tuple[Optional[Literal["global", "stream"]], Optional[str]]:
    """规范化 scope 参数；``chat`` 为 ``stream`` 的隐藏别名（不向 LLM 暴露）。"""
    raw = (scope or "global").strip().lower()
    if raw in ("global", ""):
        return "global", None
    if raw in ("stream", "chat"):
        return "stream", None
    return None, f'无效的 scope 值 "{scope}"，请使用 "global" 或 "stream"。'


def _resolve_compact_max_tokens(configured: int, size_limit: int) -> int:
    """解析压缩调用的 max_tokens。

    configured 为 0 时自动取 ``size_limit * AUTO_COMPACT_MAX_TOKENS_MULTIPLIER``。
    """
    if configured > 0:
        return configured
    return size_limit * AUTO_COMPACT_MAX_TOKENS_MULTIPLIER


def _should_retry_compact_output(
    result: dict[str, Any],
    content: str,
    size_limit: int,
) -> bool:
    """判断压缩输出是否可能因长度/token 上限被截断，从而需要翻倍 max_tokens 重试。

    优先读取 Host 返回的 ``finish_reason``；否则若返回笔记字符数低于
    ``size_limit * COMPACT_LENGTH_RETRY_RATIO``，视为可能被截断。
    """
    finish_reason = str(result.get("finish_reason") or "").strip().lower()
    if finish_reason:
        if finish_reason in _LENGTH_FINISH_REASONS:
            return True
        if "max_token" in finish_reason or finish_reason.endswith("_length"):
            return True
        return False

    if size_limit <= 0:
        return False

    min_expected_chars = int(size_limit * COMPACT_LENGTH_RETRY_RATIO)
    return len(content) < min_expected_chars


def _extract_compact_response(result: dict[str, Any]) -> str:
    """从 LLM 结果中提取压缩后的笔记正文。"""
    return (result.get("response") or "").strip()


class NoteStore:
    """便利贴笔记文件的读写封装。

    自身不持有锁逻辑，仅暴露一个 ``asyncio.Lock`` 供调用方串行化所有
    读取 / 写入 / 压缩流程，避免并发工具调用与后台压缩任务损坏文件。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    def read(self) -> str:
        """读取笔记内容；文件不存在时返回空字符串。"""
        if not self.path.exists():
            return ""
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def write(self, content: str) -> None:
        """以 UTF-8 写入（覆盖）笔记内容。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content, encoding="utf-8")

    def char_count(self) -> int:
        """返回当前笔记的字符数。"""
        return len(self.read())


class MemorySectionConfig(PluginConfigBase):
    """便利贴记忆相关配置。"""

    __ui_label__ = "便利贴记忆"
    __ui_icon__ = "sticky-note"
    __ui_order__ = 1

    size_limit: int = Field(
        default=8192,
        description="全局便利贴笔记的字符数上限（按字符计，不是字节）。超过后会触发 LLM 压缩。",
    )
    note_file: str = Field(
        default="my_memory.md",
        description="全局便利贴笔记文件名/路径；相对路径会基于插件目录解析。",
    )
    per_chat_size_limit: int = Field(
        default=4096,
        description="本聊天流便利贴笔记的字符数上限（按字符计，不是字节）。超过后会触发 LLM 压缩。",
    )
    per_chat_note_folder: str = Field(
        default="chat_notes",
        description="本聊天流便利贴存放目录；相对路径会基于插件目录解析，每个聊天流对应 <stream_id>.md。",
    )
    inject_when_empty: bool = Field(
        default=True,
        description="笔记为空时是否仍然注入提示（让麦麦知道可以给自己留备忘）。",
    )
    inject_to_planner: bool = Field(
        default=True,
        description="是否在 planner / 时机判断请求中注入便利贴。",
    )
    inject_to_replyer: bool = Field(
        default=True,
        description="是否在 replyer 回复请求中注入便利贴。",
    )
    max_compact_attempts: int = Field(
        default=3,
        description="单次压缩中，当结果仍超过上限时允许递归重压缩的最大次数（防止死循环）。",
    )
    compact_model: str = Field(
        default="planner",
        description="执行压缩时使用的 LLM 模型任务名；默认使用 planner 任务的模型。",
    )
    compact_temperature: float = Field(
        default=0.3,
        description="执行压缩时的采样温度。",
    )
    compact_max_tokens: int = Field(
        default=0,
        description=(
            "压缩 LLM 调用的最大 token 数；0 表示自动按 size_limit 的八倍计算"
            "（为推理模型的 reasoning token 预留空间）。"
            "若小于 size_limit 会在日志中告警。"
        ),
    )
    hook_timeout_ms: int = Field(
        default=DEFAULT_HOOK_TIMEOUT_MS,
        description="注入 Hook 处理器的超时时间（毫秒），默认 60000（60 秒）。",
    )
    injection_template: str = Field(
        default=DEFAULT_INJECTION_TEMPLATE,
        description=(
            "注入到系统提示词之后的模板；说明性文字默认置于【全局便利贴】之前便于模型缓存，"
            "可自行调整各段顺序。"
            "占位符：{nickname}、{note}、{used}、{free}、{size_limit}、{stream_section}。"
        ),
    )
    stream_injection_section: str = Field(
        default=DEFAULT_STREAM_INJECTION_SECTION,
        description=(
            "本聊天流便利贴注入子模板；渲染后填入 injection_template 的 {stream_section}。"
            "占位符：{stream_note}、{stream_used}、{stream_free}、{stream_size_limit}。"
        ),
    )
    compact_prompt_template: str = Field(
        default=DEFAULT_COMPACT_PROMPT_TEMPLATE,
        description=(
            "压缩笔记时发给 LLM 的提示词模板。"
            "占位符：{nickname}、{personality}、{reply_style}、{note_scope}、{size_limit}、{used}、{note}。"
        ),
    )


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class PlasticMemoryConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    memory: MemorySectionConfig = Field(default_factory=MemorySectionConfig)


class PlasticMemoryPlugin(MaiBotPlugin):
    """便利贴记忆插件主体。"""

    config_model = PlasticMemoryConfig

    def __init__(self) -> None:
        super().__init__()
        self._global_store: Optional[NoteStore] = None
        self._chat_stores: dict[str, NoteStore] = {}
        self._pending: Set[asyncio.Task] = set()
        self._plugin_dir = Path(__file__).resolve().parent
        # 配置派生缓存，on_load / on_config_update 时刷新
        self._size_limit: int = 8192
        self._per_chat_size_limit: int = 4096
        self._per_chat_note_folder: Path = self._plugin_dir / "chat_notes"
        self._inject_when_empty: bool = True
        self._inject_to_planner: bool = True
        self._inject_to_replyer: bool = True
        self._max_compact_attempts: int = 3
        self._compact_model: str = "planner"
        self._compact_temperature: float = 0.3
        self._compact_max_tokens: int = 0
        self._hook_timeout_ms: int = DEFAULT_HOOK_TIMEOUT_MS
        self._injection_template: str = DEFAULT_INJECTION_TEMPLATE
        self._stream_injection_section: str = DEFAULT_STREAM_INJECTION_SECTION
        self._compact_prompt_template: str = DEFAULT_COMPACT_PROMPT_TEMPLATE

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """插件加载：解析配置并初始化笔记存储。"""
        self._refresh_config()
        note_path = self._resolve_note_path()
        self._global_store = NoteStore(note_path)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        self._per_chat_note_folder.mkdir(parents=True, exist_ok=True)
        self.ctx.logger.info(
            "便利贴记忆插件已加载：全局笔记=%s（上限 %d 字符），聊天流笔记目录=%s（上限 %d 字符）",
            note_path,
            self._size_limit,
            self._per_chat_note_folder,
            self._per_chat_size_limit,
        )

    async def on_unload(self) -> None:
        """插件卸载：取消所有后台压缩任务。"""
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        self._chat_stores.clear()
        self.ctx.logger.info("便利贴记忆插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：刷新派生缓存与笔记路径。"""
        del config_data
        if scope == "self":
            self._refresh_config()
            note_path = self._resolve_note_path()
            chat_folder = self._resolve_chat_note_folder()
            if self._global_store is None or self._global_store.path != note_path:
                self._global_store = NoteStore(note_path)
                note_path.parent.mkdir(parents=True, exist_ok=True)
            if chat_folder != self._per_chat_note_folder:
                self._chat_stores.clear()
            self._per_chat_note_folder = chat_folder
            chat_folder.mkdir(parents=True, exist_ok=True)
            self.ctx.logger.info("便利贴记忆插件配置已更新: version=%s", version)

    def get_components(self) -> list[dict[str, Any]]:
        """收集组件，并按配置覆盖注入 Hook 的超时时间。

        ``get_components()`` 在 Runner 应用完插件配置之后、组件注册之前被调用，
        因此可以在此处读取 ``hook_timeout_ms`` 并写回 Hook 组件元数据，
        让装饰器上的静态超时值变得可配置。
        """
        components = super().get_components()
        try:
            timeout_ms = max(1, int(self.config.memory.hook_timeout_ms))
        except Exception:
            timeout_ms = DEFAULT_HOOK_TIMEOUT_MS
        for component in components:
            metadata = component.get("metadata")
            # Hook 组件的元数据里带有 "hook" 字段（订阅的 Hook 名称）
            if isinstance(metadata, dict) and "hook" in metadata:
                metadata["timeout_ms"] = timeout_ms
        return components

    # ------------------------------------------------------------------ #
    # 配置辅助
    # ------------------------------------------------------------------ #
    def _refresh_config(self) -> None:
        """从强类型配置刷新派生缓存。"""
        memory = self.config.memory
        self._size_limit = max(1, int(memory.size_limit))
        self._per_chat_size_limit = max(1, int(memory.per_chat_size_limit))
        self._per_chat_note_folder = self._resolve_chat_note_folder()
        self._inject_when_empty = bool(memory.inject_when_empty)
        self._inject_to_planner = bool(memory.inject_to_planner)
        self._inject_to_replyer = bool(memory.inject_to_replyer)
        self._max_compact_attempts = max(1, int(memory.max_compact_attempts))
        self._compact_model = (memory.compact_model or "planner").strip() or "planner"
        self._compact_temperature = float(memory.compact_temperature)
        self._compact_max_tokens = max(0, int(memory.compact_max_tokens))
        self._hook_timeout_ms = max(1, int(memory.hook_timeout_ms))
        self._injection_template = memory.injection_template or DEFAULT_INJECTION_TEMPLATE
        self._stream_injection_section = (
            memory.stream_injection_section or DEFAULT_STREAM_INJECTION_SECTION
        )
        self._compact_prompt_template = memory.compact_prompt_template or DEFAULT_COMPACT_PROMPT_TEMPLATE

    def _resolve_path_under_plugin(self, raw: str, default: str) -> Path:
        """解析相对/绝对路径，相对路径基于插件目录。"""
        candidate = Path(raw or default).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self._plugin_dir / candidate).resolve()

    def _resolve_note_path(self) -> Path:
        """解析全局笔记文件路径。"""
        return self._resolve_path_under_plugin(self.config.memory.note_file, "my_memory.md")

    def _resolve_chat_note_folder(self) -> Path:
        """解析本聊天流便利贴目录路径。"""
        return self._resolve_path_under_plugin(self.config.memory.per_chat_note_folder, "chat_notes")

    def _resolve_chat_note_path(self, stream_id: str) -> Path:
        """解析单个聊天流的便利贴文件路径。"""
        safe_name = _safe_stream_filename(stream_id)
        return self._per_chat_note_folder / f"{safe_name}.md"

    def _get_chat_store(self, stream_id: str) -> NoteStore:
        """获取（或懒创建）指定聊天流的便利贴存储。"""
        safe_name = _safe_stream_filename(stream_id)
        store = self._chat_stores.get(safe_name)
        if store is not None:
            return store
        path = self._resolve_chat_note_path(stream_id)
        store = NoteStore(path)
        self._chat_stores[safe_name] = store
        path.parent.mkdir(parents=True, exist_ok=True)
        return store

    def _resolve_target(
        self,
        scope: str,
        kwargs: dict[str, Any],
    ) -> tuple[Optional[NoteStore], int, str, Optional[str]]:
        """解析工具目标存储。

        Returns:
            (store, size_limit, scope_label, error_message)
        """
        normalized, error = _normalize_scope(scope)
        if error:
            return None, 0, "", error

        if normalized == "global":
            if self._global_store is None:
                return None, 0, "", "便利贴尚未初始化，请稍后再试。"
            return self._global_store, self._size_limit, "全局", None

        stream_id = _resolve_stream_id(kwargs)
        if not stream_id:
            return None, 0, "", "当前没有可用的聊天流上下文，无法写入聊天流便利贴。"

        store = self._get_chat_store(stream_id)
        return store, self._per_chat_size_limit, "当前聊天流", None

    def _render_stream_section(self, stream_id: str, stream_note: str) -> str:
        """渲染本聊天流便利贴注入子块。"""
        if not stream_id:
            return ""
        if not stream_note and not self._inject_when_empty:
            return ""

        used = len(stream_note)
        free = max(0, self._per_chat_size_limit - used)
        note_display = stream_note if stream_note else DEFAULT_EMPTY_STREAM_NOTE_HINT
        return _render(
            self._stream_injection_section,
            stream_note=note_display,
            stream_used=used,
            stream_free=free,
            stream_size_limit=self._per_chat_size_limit,
        )

    # ------------------------------------------------------------------ #
    # 注入 Hook：planner（含时机判断子代理）与 replyer
    # ------------------------------------------------------------------ #
    @HookHandler(
        "maisaka.planner.before_request",
        name="inject_note_planner",
        description="在 planner / 时机判断请求前，将便利贴笔记注入系统提示词之后。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=DEFAULT_HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_planner(self, **kwargs: Any) -> dict[str, Any]:
        if not self._inject_to_planner:
            return {"action": "continue"}
        return await self._inject(kwargs)

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="inject_note_replyer",
        description="在 replyer 请求前，将便利贴笔记注入系统提示词之后。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=DEFAULT_HOOK_TIMEOUT_MS,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_replyer(self, **kwargs: Any) -> dict[str, Any]:
        if not self._inject_to_replyer:
            return {"action": "continue"}
        return await self._inject(kwargs)

    async def _inject(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """把便利贴笔记作为一条 user 消息插入到 system 之后。"""
        try:
            messages = kwargs.get("messages")
            if not isinstance(messages, list):
                return {"action": "continue"}

            global_note = self._global_store.read() if self._global_store is not None else ""
            stream_id = _resolve_stream_id(kwargs)
            stream_note = ""
            if stream_id:
                stream_note = self._get_chat_store(stream_id).read()

            show_global = bool(global_note) or self._inject_when_empty
            show_stream = bool(stream_id) and (bool(stream_note) or self._inject_when_empty)
            if not show_global and not show_stream:
                return {"action": "continue"}

            stream_section = ""
            if show_stream:
                stream_section = self._render_stream_section(stream_id, stream_note)
                if stream_section:
                    stream_section = "\n" + stream_section

            nickname = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"

            if show_global:
                used = len(global_note)
                free = max(0, self._size_limit - used)
                note_display = global_note if global_note else DEFAULT_EMPTY_NOTE_HINT
            else:
                used = 0
                free = self._size_limit
                note_display = ""

            block = _render(
                self._injection_template,
                nickname=nickname,
                note=note_display,
                used=used,
                free=free,
                size_limit=self._size_limit,
                stream_section=stream_section,
            )

            # 找到最后一条 system 消息的位置，在其后插入
            insert_at = 0
            for index, item in enumerate(messages):
                if isinstance(item, dict) and item.get("role") == "system":
                    insert_at = index + 1
            messages.insert(insert_at, {"role": "user", "content": block})

            kwargs["messages"] = messages
            return {"action": "continue", "modified_kwargs": kwargs}
        except Exception as exc:  # 注入失败不应中断聊天主流程
            self.ctx.logger.warning("便利贴笔记注入失败，已跳过: %s", exc, exc_info=True)
            return {"action": "continue"}

    # ------------------------------------------------------------------ #
    # 工具：append_note / rewrite_note / compact_notes
    # ------------------------------------------------------------------ #
    @Tool(
        "append_note",
        description=(
            "把一段内容追加到你的便利贴笔记末尾，用于给自己留备忘。"
            "默认 scope=\"global\" 写入全局便利贴；scope=\"stream\" 仅写入当前聊天流便利贴（其他聊天流看不到）。"
            "建议使用 Markdown 书写（如标题、列表），但不强制。"
            "如果你的内容结尾没有换行，会自动补一个换行。"
            "注意：追加后若笔记总字符数超过上限，会在后台异步触发一次 LLM 压缩重写（compact_notes），"
            "压缩是异步的，本工具会立即返回。"
        ),
        parameters=[
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="要追加到便利贴末尾的文本内容。",
                required=True,
            ),
            SCOPE_TOOL_PARAM,
        ],
    )
    async def append_note(self, content: str, scope: str = "global", **kwargs: Any) -> dict[str, str]:
        store, size_limit, scope_label, error = self._resolve_target(scope, kwargs)
        if error:
            return {"content": error}

        async with store.lock:
            current = store.read()
            new_content = current + content
            if not new_content.endswith("\n"):
                new_content += "\n"
            store.write(new_content)
            used = len(new_content)

        over = used > size_limit
        if over:
            self._schedule_compact(store, size_limit, scope_label)
        free = max(0, size_limit - used)
        message = (
            f"已追加到{scope_label}便利贴。当前 {used} 字符，剩余可用 {free} 字符（上限 {size_limit}）。"
        )
        if over:
            message += " 笔记已超过上限，已在后台触发自动压缩。"
        return {"content": message}

    @Tool(
        "rewrite_note",
        description=(
            "用你提供的新内容【完全覆盖】便利贴笔记——旧内容会被清空。"
            "默认 scope=\"global\" 覆盖全局便利贴；scope=\"stream\" 仅覆盖当前聊天流便利贴。"
            "建议使用 Markdown 书写，但不强制。"
            "注意：如果新内容的字符数超过上限，会在后台异步触发一次 LLM 压缩重写（compact_notes），"
            "压缩是异步的，本工具会立即返回。"
        ),
        parameters=[
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="用于完全覆盖便利贴的全部新内容。",
                required=True,
            ),
            SCOPE_TOOL_PARAM,
        ],
    )
    async def rewrite_note(self, content: str, scope: str = "global", **kwargs: Any) -> dict[str, str]:
        store, size_limit, scope_label, error = self._resolve_target(scope, kwargs)
        if error:
            return {"content": error}

        async with store.lock:
            new_content = content
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            store.write(new_content)
            used = len(new_content)

        over = used > size_limit
        if over:
            self._schedule_compact(store, size_limit, scope_label)
        free = max(0, size_limit - used)
        message = (
            f"已用新内容完全覆盖{scope_label}便利贴。当前 {used} 字符，剩余可用 {free} 字符（上限 {size_limit}）。"
        )
        if over:
            message += " 笔记已超过上限，已在后台触发自动压缩。"
        return {"content": message}

    @Tool(
        "compact_notes",
        description=(
            "主动压缩便利贴笔记，让它在不被你逐行重写的情况下变得更精炼。"
            "默认 scope=\"global\" 压缩全局便利贴；scope=\"stream\" 仅压缩当前聊天流便利贴。"
            "与超限时自动触发的压缩不同，本工具是【同步阻塞】的：它会等压缩完成，"
            "并返回压缩后的字符数与剩余可用字符数。"
            "若当前笔记未超过上限，则不会改动内容。"
        ),
        parameters=[SCOPE_TOOL_PARAM],
    )
    async def compact_notes(self, scope: str = "global", **kwargs: Any) -> dict[str, str]:
        store, size_limit, scope_label, error = self._resolve_target(scope, kwargs)
        if error:
            return {"content": error}

        count = await self._compact(store, size_limit, scope_label)
        free = max(0, size_limit - count)
        return {
            "content": (
                f"{scope_label}便利贴压缩完成。当前 {count} 字符，剩余可用 {free} 字符（上限 {size_limit}）。"
            )
        }

    # ------------------------------------------------------------------ #
    # 压缩实现
    # ------------------------------------------------------------------ #
    def _schedule_compact(self, store: NoteStore, size_limit: int, scope_label: str) -> None:
        """调度一个后台压缩任务（非阻塞）。"""
        task = asyncio.create_task(self._safe_compact(store, size_limit, scope_label))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _safe_compact(self, store: NoteStore, size_limit: int, scope_label: str) -> None:
        """后台压缩包装：吞掉异常仅记录日志。"""
        try:
            await self._compact(store, size_limit, scope_label)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("%s便利贴后台压缩失败: %s", scope_label, exc, exc_info=True)

    async def _generate_compact_llm(self, prompt: str, max_tokens: int, size_limit: int) -> dict[str, Any]:
        """调用 LLM 执行压缩；必要时翻倍 max_tokens 重试，并在多次结果中取最长笔记。"""
        current_max = max_tokens
        collected: list[tuple[dict[str, Any], str]] = []
        result: dict[str, Any] = {"success": False, "response": ""}

        for call_index in range(MAX_COMPACT_LENGTH_RETRIES + 1):
            result = await self.ctx.llm.generate(
                prompt=prompt,
                model=self._compact_model,
                temperature=self._compact_temperature,
                max_tokens=current_max,
            )
            content = _extract_compact_response(result)
            if result.get("success") and content:
                collected.append((result, content))

            if call_index >= MAX_COMPACT_LENGTH_RETRIES:
                break
            if not result.get("success"):
                break
            if not _should_retry_compact_output(result, content, size_limit):
                break

            previous_max = current_max
            current_max *= 2
            self.ctx.logger.info(
                "便利贴压缩输出可能因长度触顶被截断"
                "（finish_reason=%s, 返回 %d 字符, 阈值 %d 字符, max_tokens=%d），"
                "临时将 max_tokens 提升至 %d 并重试（第 %d/%d 次）",
                result.get("finish_reason") or "未提供/启发式",
                len(content),
                int(size_limit * COMPACT_LENGTH_RETRY_RATIO),
                previous_max,
                current_max,
                call_index + 1,
                MAX_COMPACT_LENGTH_RETRIES,
            )

        if collected:
            best_result, best_content = max(collected, key=lambda item: len(item[1]))
            if len(collected) > 1:
                self.ctx.logger.info(
                    "便利贴压缩在 %d 次调用后选用最长响应（%d 字符）",
                    len(collected),
                    len(best_content),
                )
            merged = dict(best_result)
            merged["response"] = best_content
            return merged

        return result

    async def _compact(self, store: NoteStore, size_limit: int, scope_label: str) -> int:
        """对便利贴执行一次（可能递归的）LLM 压缩，返回压缩后的字符数。"""
        async with store.lock:
            content = store.read()
            if len(content) <= size_limit:
                return len(content)

            nickname = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"
            personality = await self.ctx.config.get("personality.personality", "") or ""
            reply_style = await self.ctx.config.get("personality.reply_style", "") or ""

            max_tokens = _resolve_compact_max_tokens(self._compact_max_tokens, size_limit)
            if self._compact_max_tokens > 0 and max_tokens < size_limit:
                self.ctx.logger.warning(
                    "%s便利贴压缩 max_tokens(%d) 小于 size_limit(%d)，可能无法压缩到目标长度",
                    scope_label,
                    max_tokens,
                    size_limit,
                )

            best = content
            for attempt in range(1, self._max_compact_attempts + 1):
                prompt = _render(
                    self._compact_prompt_template,
                    nickname=nickname,
                    personality=personality,
                    reply_style=reply_style,
                    note_scope=scope_label,
                    size_limit=size_limit,
                    used=len(content),
                    note=content,
                )
                result = await self._generate_compact_llm(prompt, max_tokens, size_limit)
                if not result.get("success"):
                    self.ctx.logger.warning(
                        "%s便利贴压缩第 %d 次 LLM 调用失败: %s",
                        scope_label,
                        attempt,
                        result.get("error") or result.get("response") or "未知错误",
                    )
                    break

                new_content = _extract_compact_response(result)
                if not new_content:
                    self.ctx.logger.warning(
                        "%s便利贴压缩第 %d 次返回空内容，停止压缩",
                        scope_label,
                        attempt,
                    )
                    break

                if len(new_content) < len(best):
                    best = new_content
                content = new_content

                if len(new_content) <= size_limit:
                    break

                self.ctx.logger.info(
                    "%s便利贴压缩第 %d 次仍超限（%d > %d），继续压缩",
                    scope_label,
                    attempt,
                    len(new_content),
                    size_limit,
                )

            if best and not best.endswith("\n"):
                best += "\n"
            store.write(best)
            self.ctx.logger.info(
                "%s便利贴压缩完成，当前 %d 字符（上限 %d）",
                scope_label,
                len(best),
                size_limit,
            )
            return len(best)


def create_plugin() -> PlasticMemoryPlugin:
    """创建插件实例。"""
    return PlasticMemoryPlugin()
