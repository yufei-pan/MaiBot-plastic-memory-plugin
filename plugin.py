"""塑料内存条 (Plastic Memory) 插件。

为麦麦提供一张可自行管理的便利贴笔记（my_memory.md）：
- 在每次 planner / 时机判断 / replyer 请求的系统提示词之后注入笔记内容与剩余空间；
- 暴露 append_note / rewrite_note / compact_notes 三个工具供麦麦自行维护笔记；
- 当笔记超过 size_limit（字符数）时，自动或主动触发基于 LLM 的压缩重写。
"""

import asyncio
from pathlib import Path
from typing import Any, Optional, Set

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType


# 默认的注入模板，可在 config.toml 中覆盖。
# 占位符：{note}、{used}、{free}、{size_limit}
DEFAULT_INJECTION_TEMPLATE = """【便利贴笔记】
以下是你之前给自己留下的便利贴笔记，请在思考与回复时参考。
如需更新，可调用 append_note（追加）、rewrite_note（整体重写）或 compact_notes（压缩）工具：

{note}

（便利贴当前已用 {used} 字符，剩余可用 {free} 字符，上限 {size_limit} 字符）"""

# 当笔记为空时，用于填充 {note} 占位符的提示文本。
DEFAULT_EMPTY_NOTE_HINT = "（便利贴当前为空，你可以用 append_note 给自己留备忘）"

# HookHandler 处理器的默认超时时间（毫秒）。可通过 config.toml 的 hook_timeout_ms 覆盖。
DEFAULT_HOOK_TIMEOUT_MS = 60000

# 默认的压缩提示词模板，可在 config.toml 中覆盖。
# 占位符：{nickname}、{personality}、{reply_style}、{size_limit}、{used}、{note}
DEFAULT_COMPACT_PROMPT_TEMPLATE = """你是{nickname}。
你的人格设定：{personality}
你的表达风格：{reply_style}

下面这份"便利贴笔记"是你写给自己的备忘，但它太长了：当前 {used} 字符，必须压缩到 {size_limit} 字符以内。
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
        description="便利贴笔记的字符数上限（按字符计，不是字节）。超过后会触发 LLM 压缩。",
    )
    note_file: str = Field(
        default="my_memory.md",
        description="便利贴笔记文件名/路径；相对路径会基于插件目录解析。",
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
        description="压缩 LLM 调用的最大 token 数；0 表示自动按 size_limit 的两倍计算。若小于 size_limit 会在日志中告警。",
    )
    hook_timeout_ms: int = Field(
        default=DEFAULT_HOOK_TIMEOUT_MS,
        description="注入 Hook 处理器的超时时间（毫秒），默认 60000（60 秒）。",
    )
    injection_template: str = Field(
        default=DEFAULT_INJECTION_TEMPLATE,
        description="注入到系统提示词之后的模板。占位符：{note}、{used}、{free}、{size_limit}。",
    )
    compact_prompt_template: str = Field(
        default=DEFAULT_COMPACT_PROMPT_TEMPLATE,
        description="压缩笔记时发给 LLM 的提示词模板。占位符：{nickname}、{personality}、{reply_style}、{size_limit}、{used}、{note}。",
    )


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class PlasticMemoryConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    memory: MemorySectionConfig = Field(default_factory=MemorySectionConfig)


class PlasticMemoryPlugin(MaiBotPlugin):
    """便利贴记忆插件主体。"""

    config_model = PlasticMemoryConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[NoteStore] = None
        self._pending: Set[asyncio.Task] = set()
        # 配置派生缓存，on_load / on_config_update 时刷新
        self._size_limit: int = 8192
        self._inject_when_empty: bool = True
        self._inject_to_planner: bool = True
        self._inject_to_replyer: bool = True
        self._max_compact_attempts: int = 3
        self._compact_model: str = "planner"
        self._compact_temperature: float = 0.3
        self._compact_max_tokens: int = 0
        self._hook_timeout_ms: int = DEFAULT_HOOK_TIMEOUT_MS
        self._injection_template: str = DEFAULT_INJECTION_TEMPLATE
        self._compact_prompt_template: str = DEFAULT_COMPACT_PROMPT_TEMPLATE

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """插件加载：解析配置并初始化笔记存储。"""
        self._refresh_config()
        note_path = self._resolve_note_path()
        self._store = NoteStore(note_path)
        # 确保父目录存在，但不强制创建空文件
        note_path.parent.mkdir(parents=True, exist_ok=True)
        self.ctx.logger.info(
            "便利贴记忆插件已加载：笔记文件=%s，上限=%d 字符", note_path, self._size_limit
        )

    async def on_unload(self) -> None:
        """插件卸载：取消所有后台压缩任务。"""
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()
        self.ctx.logger.info("便利贴记忆插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热更新：刷新派生缓存与笔记路径。"""
        del config_data
        if scope == "self":
            self._refresh_config()
            note_path = self._resolve_note_path()
            if self._store is None or self._store.path != note_path:
                self._store = NoteStore(note_path)
                note_path.parent.mkdir(parents=True, exist_ok=True)
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
        self._inject_when_empty = bool(memory.inject_when_empty)
        self._inject_to_planner = bool(memory.inject_to_planner)
        self._inject_to_replyer = bool(memory.inject_to_replyer)
        self._max_compact_attempts = max(1, int(memory.max_compact_attempts))
        self._compact_model = (memory.compact_model or "planner").strip() or "planner"
        self._compact_temperature = float(memory.compact_temperature)
        self._compact_max_tokens = max(0, int(memory.compact_max_tokens))
        self._hook_timeout_ms = max(1, int(memory.hook_timeout_ms))
        self._injection_template = memory.injection_template or DEFAULT_INJECTION_TEMPLATE
        self._compact_prompt_template = memory.compact_prompt_template or DEFAULT_COMPACT_PROMPT_TEMPLATE

    def _resolve_note_path(self) -> Path:
        """解析笔记文件路径，相对路径基于插件目录。"""
        raw = self.config.memory.note_file or "my_memory.md"
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
        return (Path(__file__).resolve().parent / candidate).resolve()

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

            note = self._store.read() if self._store is not None else ""
            if not note and not self._inject_when_empty:
                return {"action": "continue"}

            used = len(note)
            free = max(0, self._size_limit - used)
            note_display = note if note else DEFAULT_EMPTY_NOTE_HINT
            block = _render(
                self._injection_template,
                note=note_display,
                used=used,
                free=free,
                size_limit=self._size_limit,
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
            "把一段内容追加到你的便利贴笔记（my_memory.md）末尾，用于给自己留备忘。"
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
        ],
    )
    async def append_note(self, content: str, **kwargs: Any) -> dict[str, str]:
        del kwargs
        if self._store is None:
            return {"content": "便利贴尚未初始化，请稍后再试。"}

        async with self._store.lock:
            current = self._store.read()
            new_content = current + content
            if not new_content.endswith("\n"):
                new_content += "\n"
            self._store.write(new_content)
            used = len(new_content)

        over = used > self._size_limit
        if over:
            self._schedule_compact()
        free = max(0, self._size_limit - used)
        message = f"已追加到便利贴。当前 {used} 字符，剩余可用 {free} 字符（上限 {self._size_limit}）。"
        if over:
            message += " 笔记已超过上限，已在后台触发自动压缩。"
        return {"content": message}

    @Tool(
        "rewrite_note",
        description=(
            "用你提供的新内容【完全覆盖】整张便利贴笔记（my_memory.md）——旧内容会被清空。"
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
        ],
    )
    async def rewrite_note(self, content: str, **kwargs: Any) -> dict[str, str]:
        del kwargs
        if self._store is None:
            return {"content": "便利贴尚未初始化，请稍后再试。"}

        async with self._store.lock:
            new_content = content
            if new_content and not new_content.endswith("\n"):
                new_content += "\n"
            self._store.write(new_content)
            used = len(new_content)

        over = used > self._size_limit
        if over:
            self._schedule_compact()
        free = max(0, self._size_limit - used)
        message = f"已用新内容完全覆盖便利贴。当前 {used} 字符，剩余可用 {free} 字符（上限 {self._size_limit}）。"
        if over:
            message += " 笔记已超过上限，已在后台触发自动压缩。"
        return {"content": message}

    @Tool(
        "compact_notes",
        description=(
            "主动压缩便利贴笔记，让它在不被你逐行重写的情况下变得更精炼。"
            "与超限时自动触发的压缩不同，本工具是【同步阻塞】的：它会等压缩完成，"
            "并返回压缩后的字符数与剩余可用字符数。"
            "若当前笔记未超过上限，则不会改动内容。"
        ),
    )
    async def compact_notes(self, **kwargs: Any) -> dict[str, str]:
        del kwargs
        if self._store is None:
            return {"content": "便利贴尚未初始化，请稍后再试。"}

        count = await self._compact()
        free = max(0, self._size_limit - count)
        return {
            "content": f"压缩完成。当前 {count} 字符，剩余可用 {free} 字符（上限 {self._size_limit}）。"
        }

    # ------------------------------------------------------------------ #
    # 压缩实现
    # ------------------------------------------------------------------ #
    def _schedule_compact(self) -> None:
        """调度一个后台压缩任务（非阻塞）。"""
        task = asyncio.create_task(self._safe_compact())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _safe_compact(self) -> None:
        """后台压缩包装：吞掉异常仅记录日志。"""
        try:
            await self._compact()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("便利贴后台压缩失败: %s", exc, exc_info=True)

    async def _compact(self) -> int:
        """对便利贴执行一次（可能递归的）LLM 压缩，返回压缩后的字符数。"""
        if self._store is None:
            return 0

        async with self._store.lock:
            content = self._store.read()
            if len(content) <= self._size_limit:
                return len(content)

            nickname = await self.ctx.config.get("bot.nickname", "麦麦") or "麦麦"
            personality = await self.ctx.config.get("personality.personality", "") or ""
            reply_style = await self.ctx.config.get("personality.reply_style", "") or ""

            # max_tokens：未配置（0）时自动取 size_limit 的两倍；配置值小于上限时告警
            if self._compact_max_tokens > 0:
                max_tokens = self._compact_max_tokens
                if max_tokens < self._size_limit:
                    self.ctx.logger.warning(
                        "便利贴压缩 max_tokens(%d) 小于 size_limit(%d)，可能无法压缩到目标长度",
                        max_tokens,
                        self._size_limit,
                    )
            else:
                max_tokens = self._size_limit * 2

            best = content
            for attempt in range(1, self._max_compact_attempts + 1):
                prompt = _render(
                    self._compact_prompt_template,
                    nickname=nickname,
                    personality=personality,
                    reply_style=reply_style,
                    size_limit=self._size_limit,
                    used=len(content),
                    note=content,
                )
                result = await self.ctx.llm.generate(
                    prompt=prompt,
                    model=self._compact_model,
                    temperature=self._compact_temperature,
                    max_tokens=max_tokens,
                )
                if not result.get("success"):
                    self.ctx.logger.warning(
                        "便利贴压缩第 %d 次 LLM 调用失败: %s",
                        attempt,
                        result.get("error") or result.get("response") or "未知错误",
                    )
                    break

                new_content = (result.get("response") or "").strip()
                if not new_content:
                    self.ctx.logger.warning("便利贴压缩第 %d 次返回空内容，停止压缩", attempt)
                    break

                if len(new_content) < len(best):
                    best = new_content
                content = new_content

                if len(new_content) <= self._size_limit:
                    break

                self.ctx.logger.info(
                    "便利贴压缩第 %d 次仍超限（%d > %d），继续压缩",
                    attempt,
                    len(new_content),
                    self._size_limit,
                )

            if best and not best.endswith("\n"):
                best += "\n"
            self._store.write(best)
            self.ctx.logger.info("便利贴压缩完成，当前 %d 字符（上限 %d）", len(best), self._size_limit)
            return len(best)


def create_plugin() -> PlasticMemoryPlugin:
    """创建插件实例。"""
    return PlasticMemoryPlugin()
