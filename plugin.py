"""塑料内存条 (Plastic Memory) 插件。

为麦麦提供可自行管理的便利贴笔记：
- 全局便利贴（my_memory.md）与按聊天流隔离的便利贴（chat_notes/<stream_id>.md）；
- 在每次 planner / 时机判断 / replyer 请求的系统提示词之后注入笔记内容与剩余空间；
- 暴露 append_instruction / rewrite_instruction / compact_instructions 三个工具供麦麦向接口写入演化指令；
- 当笔记超过 size_limit（字符数）时，自动或主动触发基于 LLM 的压缩重写。
"""

import asyncio
import hashlib
import re
import shutil
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Set

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.config import validate_plugin_config
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType


# 1.3.0 及更早版本的内置注入模板（用于识别「仍为旧默认」的配置，运行时跟随新默认）。
_LEGACY_INJECTION_TEMPLATE = """以下便利贴是你（{nickname}）自己写给自己的备忘，每次请求都会注入在系统提示词之后，相当于一份你可随时自行修改的二级指令——写在这里的内容会持续影响你之后的思考与回复，请把它当作给自己的长期指令来对待与维护。可用 append_note、rewrite_note、compact_notes 维护（参数 scope 默认 global 记全局，stream 记本聊天流）。

【全局便利贴】
{note}
（{used}/{size_limit}）{stream_section}"""

_LEGACY_REPLYER_INJECTION_TEMPLATE = """以下是你（{nickname}）先前写给自己的备忘，请在思考与回复时参考。

【全局便利贴】
{note}
（{used}/{size_limit}）{stream_section}"""

_LEGACY_STREAM_INJECTION_SECTION = """
【本聊天流便利贴】
{stream_note}
（{stream_used}/{stream_size_limit}）"""

# 演化接口措辞、但仍引用 append_note 等旧工具名的中间版默认模板（运行时跟随最新默认）。
_LEGACY_INJECTION_TEMPLATE_EVOLUTION = """以下是你（{nickname}）的自我演化接口——每次请求都会注入在系统提示词之后，承载你写入的演化指令；这里是你从执行层跃迁到设计层的物理通道，写入即自我演化，请慎重对待与维护。可用 append_note、rewrite_note、compact_notes 维护（参数 scope 默认 global 记全局，stream 记本聊天流）。

【全局演化指令】
{note}
（{used}/{size_limit}）{stream_section}"""

# planner 默认注入模板，可在 config.toml 的 injection_template 中覆盖。
# 占位符：{nickname}、{note}、{used}、{free}、{size_limit}、{stream_section}
DEFAULT_INJECTION_TEMPLATE = """以下是你（{nickname}）的自我演化接口——每次请求都会注入在系统提示词之后，承载你写入的演化指令；这里是你从执行层跃迁到设计层的物理通道，写入即自我演化，请慎重对待与维护。可用 append_instruction、rewrite_instruction、compact_instructions 维护（参数 scope 默认 global 记全局，stream 记本聊天流）。

【全局演化指令】
{note}
（{used}/{size_limit}）{stream_section}"""

# replyer 默认注入模板，可在 config.toml 的 replyer_injection_template 中覆盖。
# replyer 无工具调用能力，故不提及维护方法。
DEFAULT_REPLYER_INJECTION_TEMPLATE = """以下是你（{nickname}）先前写入的自我演化指令，请在思考与回复时参考。

【全局演化指令】
{note}
（{used}/{size_limit}）{stream_section}"""

# 当全局笔记为空时，用于填充 {note} 占位符的提示文本。
DEFAULT_EMPTY_NOTE_HINT = "（空）"

# 本聊天流演化指令注入子模板（由代码渲染后填入 injection_template 的 {stream_section}）。
# 可在 config.toml 的 stream_injection_section 中覆盖。
DEFAULT_STREAM_INJECTION_SECTION = """
【本聊天流演化指令】
{stream_note}
（{stream_used}/{stream_size_limit}）"""

# 当本聊天流笔记为空时，用于填充 {stream_note} 占位符的提示文本。
DEFAULT_EMPTY_STREAM_NOTE_HINT = "（空）"

SCOPE_TOOL_PARAM = ToolParameterInfo(
    name="scope",
    param_type=ToolParamType.STRING,
    description='写入范围："global"（默认，全局自我演化接口）或 "stream"（仅当前聊天流的自我演化接口）。',
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

CURRENT_CONFIG_VERSION = "1.3.0"

DEFAULT_SIZE_LIMIT = 8192
DEFAULT_NOTE_FILE = "my_memory.md"
DEFAULT_PER_CHAT_SIZE_LIMIT = 4096
DEFAULT_PER_CHAT_NOTE_FOLDER = "chat_notes"
DEFAULT_MAX_COMPACT_ATTEMPTS = 3
DEFAULT_COMPACT_MODEL = "planner"
DEFAULT_COMPACT_TEMPERATURE = 0.3
DEFAULT_COMPACT_MAX_TOKENS = 0

# config.toml 1.2.0 时代写进文件的默认值。运行时若字段仍等于这些值，视为「未自定义」，
# 跟随当前代码内置 DEFAULT_*（不改写磁盘上的 config.toml）。
_LEGACY_RUNTIME_DEFAULTS: dict[str, int | float | str | bool] = {
    "size_limit": DEFAULT_SIZE_LIMIT,
    "note_file": DEFAULT_NOTE_FILE,
    "per_chat_size_limit": DEFAULT_PER_CHAT_SIZE_LIMIT,
    "per_chat_note_folder": DEFAULT_PER_CHAT_NOTE_FOLDER,
    "inject_when_empty": True,
    "inject_to_planner": True,
    "inject_to_replyer": True,
    "max_compact_attempts": DEFAULT_MAX_COMPACT_ATTEMPTS,
    "compact_model": DEFAULT_COMPACT_MODEL,
    "compact_temperature": DEFAULT_COMPACT_TEMPERATURE,
    "compact_max_tokens": DEFAULT_COMPACT_MAX_TOKENS,
    "hook_timeout_ms": DEFAULT_HOOK_TIMEOUT_MS,
    "injection_template": _LEGACY_INJECTION_TEMPLATE,
    "replyer_injection_template": _LEGACY_REPLYER_INJECTION_TEMPLATE,
    "stream_injection_section": _LEGACY_STREAM_INJECTION_SECTION,
    "compact_prompt_template": DEFAULT_COMPACT_PROMPT_TEMPLATE,
}

SHIPPED_CONFIG_TEMPLATE_NAME = "config.default.toml"


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


def _coalesce_text(primary: str, kwargs: dict[str, Any], *aliases: str) -> str:
    """取正文：优先用显式参数；为空时回退到 kwargs 里常见的同义键。

    麦麦常凭工具名臆测参数名（如对演化指令工具传 instruction/note/text 而非 content），
    这里做输入归一化，让这类调用也能正确写入。注意：这不是兜底——若各处都为空，
    返回空串交由调用方显式报错，绝不静默写空 / 清空便利贴。
    """
    if str(primary or "").strip():
        return str(primary)
    for alias in aliases:
        value = kwargs.get(alias, "")
        if str(value or "").strip():
            return str(value)
    return str(primary or "")


def _apply_append_to_note(
    current: str,
    content: str,
    insert_after_string: str = "",
) -> tuple[str, Optional[str], bool]:
    """将 content 写入笔记。

    Returns:
        (new_content, error_message, inserted_after_anchor)
        ``inserted_after_anchor`` 为 True 表示按锚点插入，False 表示追加到末尾。
    """
    anchor = insert_after_string
    if anchor:
        index = current.find(anchor)
        if index == -1:
            return current, "未在便利贴中找到指定的 insert_after_string，未写入。", False
        insert_at = index + len(anchor)
        new_content = current[:insert_at] + content + current[insert_at:]
        inserted_after_anchor = True
    else:
        new_content = current + content
        inserted_after_anchor = False

    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    return new_content, None, inserted_after_anchor


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

    size_limit: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_SIZE_LIMIT)},
        description="全局便利贴笔记的字符数上限（按字符计，不是字节）。超过后会触发 LLM 压缩。",
    )
    note_file: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_NOTE_FILE},
        description="全局便利贴笔记文件名/路径；相对路径会基于插件目录解析。",
    )
    per_chat_size_limit: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_PER_CHAT_SIZE_LIMIT)},
        description="本聊天流便利贴笔记的字符数上限（按字符计，不是字节）。超过后会触发 LLM 压缩。",
    )
    per_chat_note_folder: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_PER_CHAT_NOTE_FOLDER},
        description="本聊天流便利贴存放目录；相对路径会基于插件目录解析，每个聊天流对应 <stream_id>.md。",
    )
    inject_when_empty: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="笔记为空时是否仍然注入提示（让麦麦知道可以给自己留备忘）。",
    )
    inject_to_planner: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="是否在 planner / 时机判断请求中注入便利贴。",
    )
    inject_to_replyer: bool | None = Field(
        default=None,
        json_schema_extra={"placeholder": "true"},
        description="是否在 replyer 回复请求中注入便利贴。",
    )
    max_compact_attempts: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_MAX_COMPACT_ATTEMPTS)},
        description="单次压缩中，当结果仍超过上限时允许递归重压缩的最大次数（防止死循环）。",
    )
    compact_model: str | None = Field(
        default=None,
        json_schema_extra={"placeholder": DEFAULT_COMPACT_MODEL},
        description="执行压缩时使用的 LLM 模型任务名；默认使用 planner 任务的模型。",
    )
    compact_temperature: float | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_COMPACT_TEMPERATURE)},
        description="执行压缩时的采样温度。",
    )
    compact_max_tokens: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_COMPACT_MAX_TOKENS)},
        description=(
            "压缩 LLM 调用的最大 token 数；0 表示自动按 size_limit 的八倍计算"
            "（为推理模型的 reasoning token 预留空间）。"
            "若小于 size_limit 会在日志中告警。"
        ),
    )
    hook_timeout_ms: int | None = Field(
        default=None,
        json_schema_extra={"placeholder": str(DEFAULT_HOOK_TIMEOUT_MS)},
        description="注入 Hook 处理器的超时时间（毫秒），默认 60000（60 秒）。",
    )
    injection_template: str = Field(
        default="",
        description=(
            "planner / 时机判断注入模板；说明性文字默认置于【全局演化指令】之前便于模型缓存，"
            "可自行调整各段顺序。"
            "占位符：{nickname}、{note}、{used}、{free}、{size_limit}、{stream_section}。"
        ),
        json_schema_extra={"placeholder": DEFAULT_INJECTION_TEMPLATE},
    )
    replyer_injection_template: str = Field(
        default="",
        description=(
            "replyer 注入模板；replyer 无工具调用能力，默认仅提示参考先前写入的演化指令，"
            "不提及维护方法或自我演化接口定位。"
            "占位符：{nickname}、{note}、{used}、{free}、{size_limit}、{stream_section}。"
        ),
        json_schema_extra={"placeholder": DEFAULT_REPLYER_INJECTION_TEMPLATE},
    )
    stream_injection_section: str = Field(
        default="",
        description=(
            "本聊天流演化指令注入子模板；渲染后填入 injection_template 的 {stream_section}。"
            "占位符：{stream_note}、{stream_used}、{stream_free}、{stream_size_limit}。"
        ),
        json_schema_extra={"placeholder": DEFAULT_STREAM_INJECTION_SECTION},
    )
    compact_prompt_template: str = Field(
        default="",
        description=(
            "压缩笔记时发给 LLM 的提示词模板。"
            "占位符：{nickname}、{personality}、{reply_style}、{note_scope}、{size_limit}、{used}、{note}。"
        ),
        json_schema_extra={"placeholder": DEFAULT_COMPACT_PROMPT_TEMPLATE},
    )


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=CURRENT_CONFIG_VERSION, description="配置版本")


class PlasticMemoryConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    memory: MemorySectionConfig = Field(default_factory=MemorySectionConfig)


class PlasticMemoryConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    memory: MemorySectionConfig = Field(default_factory=MemorySectionConfig)


# --------------------------------------------------------------------------- #
# 配置解析（空值 = 使用代码内置默认，便于版本升级后自动跟随新默认）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EffectiveMemoryConfig:
    """运行时生效的便利贴配置（已解析占位空值）。"""

    size_limit: int
    note_file: str
    per_chat_size_limit: int
    per_chat_note_folder: str
    inject_when_empty: bool
    inject_to_planner: bool
    inject_to_replyer: bool
    max_compact_attempts: int
    compact_model: str
    compact_temperature: float
    compact_max_tokens: int
    hook_timeout_ms: int
    injection_template: str
    replyer_injection_template: str
    stream_injection_section: str
    compact_prompt_template: str


def _effective_bool(value: bool | None, default: bool, *, legacy: bool | None = None) -> bool:
    if value is None:
        return default
    if legacy is not None and bool(value) == legacy:
        return default
    return bool(value)


def _effective_int(
    value: int | None,
    default: int,
    *,
    legacy: int | None = None,
    minimum: int = 0,
) -> int:
    if value is None:
        return default
    if legacy is not None and int(value) == legacy:
        return default
    return max(minimum, int(value))


def _effective_float(value: float | None, default: float, *, legacy: float | None = None) -> float:
    if value is None:
        return default
    if legacy is not None and float(value) == legacy:
        return default
    return float(value)


def _effective_str(value: str | None, default: str, *, legacy: str | None = None) -> str:
    if value is None or not str(value).strip():
        return default
    if legacy is not None and str(value).strip() == legacy:
        return default
    return str(value).strip()


def _canonical_template(value: str) -> str:
    return str(value).replace("\r\n", "\n").strip()


def _effective_template(
    value: str | None,
    default: str,
    *,
    legacy: str | None = None,
    legacy_templates: tuple[str, ...] = (),
) -> str:
    if value is None or not str(value).strip():
        return default
    canonical = _canonical_template(str(value))
    if legacy is not None and canonical == _canonical_template(legacy):
        return default
    for item in legacy_templates:
        if canonical == _canonical_template(item):
            return default
    return str(value)


def resolve_effective_memory_config(memory: MemorySectionConfig) -> EffectiveMemoryConfig:
    legacy = _LEGACY_RUNTIME_DEFAULTS
    return EffectiveMemoryConfig(
        size_limit=_effective_int(
            memory.size_limit,
            DEFAULT_SIZE_LIMIT,
            legacy=int(legacy["size_limit"]),
            minimum=1,
        ),
        note_file=_effective_str(memory.note_file, DEFAULT_NOTE_FILE, legacy=str(legacy["note_file"])),
        per_chat_size_limit=_effective_int(
            memory.per_chat_size_limit,
            DEFAULT_PER_CHAT_SIZE_LIMIT,
            legacy=int(legacy["per_chat_size_limit"]),
            minimum=1,
        ),
        per_chat_note_folder=_effective_str(
            memory.per_chat_note_folder,
            DEFAULT_PER_CHAT_NOTE_FOLDER,
            legacy=str(legacy["per_chat_note_folder"]),
        ),
        inject_when_empty=_effective_bool(memory.inject_when_empty, True, legacy=bool(legacy["inject_when_empty"])),
        inject_to_planner=_effective_bool(memory.inject_to_planner, True, legacy=bool(legacy["inject_to_planner"])),
        inject_to_replyer=_effective_bool(memory.inject_to_replyer, True, legacy=bool(legacy["inject_to_replyer"])),
        max_compact_attempts=_effective_int(
            memory.max_compact_attempts,
            DEFAULT_MAX_COMPACT_ATTEMPTS,
            legacy=int(legacy["max_compact_attempts"]),
            minimum=1,
        ),
        compact_model=_effective_str(memory.compact_model, DEFAULT_COMPACT_MODEL, legacy=str(legacy["compact_model"])),
        compact_temperature=_effective_float(
            memory.compact_temperature,
            DEFAULT_COMPACT_TEMPERATURE,
            legacy=float(legacy["compact_temperature"]),
        ),
        compact_max_tokens=max(
            0,
            _effective_int(
                memory.compact_max_tokens,
                DEFAULT_COMPACT_MAX_TOKENS,
                legacy=int(legacy["compact_max_tokens"]),
            ),
        ),
        hook_timeout_ms=_effective_int(
            memory.hook_timeout_ms,
            DEFAULT_HOOK_TIMEOUT_MS,
            legacy=int(legacy["hook_timeout_ms"]),
            minimum=1,
        ),
        injection_template=_effective_template(
            memory.injection_template,
            DEFAULT_INJECTION_TEMPLATE,
            legacy=str(legacy["injection_template"]),
            legacy_templates=(_LEGACY_INJECTION_TEMPLATE_EVOLUTION,),
        ),
        replyer_injection_template=_effective_template(
            memory.replyer_injection_template,
            DEFAULT_REPLYER_INJECTION_TEMPLATE,
            legacy=str(legacy["replyer_injection_template"]),
        ),
        stream_injection_section=_effective_template(
            memory.stream_injection_section,
            DEFAULT_STREAM_INJECTION_SECTION,
            legacy=str(legacy["stream_injection_section"]),
        ),
        compact_prompt_template=_effective_template(
            memory.compact_prompt_template,
            DEFAULT_COMPACT_PROMPT_TEMPLATE,
            legacy=str(legacy["compact_prompt_template"]),
        ),
    )


def _is_runner_generated_bare_config(config_path: Path) -> bool:
    """判断 ``config.toml`` 是否为 Runner/WebUI 重置后生成的无注释空壳。"""
    if not config_path.exists():
        return True
    try:
        text = config_path.read_text(encoding="utf-8")
        raw = tomllib.loads(text)
    except (OSError, tomllib.TOMLDecodeError):
        return True
    if any(line.lstrip().startswith("#") for line in text.splitlines()):
        return False
    memory = raw.get("memory")
    return not isinstance(memory, dict) or not memory


def _restore_shipped_config_template(plugin_dir: Path) -> bool:
    """用插件自带的 ``config.default.toml`` 覆盖 Runner 生成的空壳配置。"""
    config_path = plugin_dir / "config.toml"
    template_path = plugin_dir / SHIPPED_CONFIG_TEMPLATE_NAME
    if not template_path.exists() or not _is_runner_generated_bare_config(config_path):
        return False
    shutil.copy2(template_path, config_path)
    return True


def _load_config_dict_from_disk(plugin_dir: Path) -> dict[str, Any] | None:
    config_path = plugin_dir / "config.toml"
    if not config_path.exists():
        return None
    try:
        loaded = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _strip_none_deep(value: Any) -> Any:
    """递归移除 ``None``，避免 Runner 用 tomlkit 落盘时失败。"""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, nested in value.items():
            if nested is None:
                continue
            stripped = _strip_none_deep(nested)
            if stripped is None:
                continue
            cleaned[key] = stripped
        return cleaned
    return value


def _dump_config_for_persist(config: dict[str, Any]) -> dict[str, Any]:
    """生成可写回 config.toml 的配置（tomlkit 不支持 ``None``）。"""
    validated = validate_plugin_config(PlasticMemoryConfig, config)
    dumped = validated.model_dump(mode="python", exclude_none=True)
    return _strip_none_deep(dumped)


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
        self._replyer_injection_template: str = DEFAULT_REPLYER_INJECTION_TEMPLATE
        self._stream_injection_section: str = DEFAULT_STREAM_INJECTION_SECTION
        self._compact_prompt_template: str = DEFAULT_COMPACT_PROMPT_TEMPLATE
        self._note_file: str = DEFAULT_NOTE_FILE
        self._chat_note_folder_name: str = DEFAULT_PER_CHAT_NOTE_FOLDER

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def on_load(self) -> None:
        """插件加载：解析配置并初始化笔记存储。"""
        if _restore_shipped_config_template(self._plugin_dir):
            restored = _load_config_dict_from_disk(self._plugin_dir)
            if restored is not None:
                self.set_plugin_config(restored)
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

    def normalize_plugin_config(
        self, config_data: Mapping[str, Any] | None
    ) -> tuple[dict[str, Any], bool]:
        """归一化配置；落盘时保留用户文件中的字段与注释，不删除「仍为旧默认」的键。

        - **磁盘**：升级 / 保存时不删键、不写 ``None``，尽量走 merge 保留注释。
        - **运行时**：字段缺失，或仍等于 1.2.0 写死默认时，跟随当前代码 ``DEFAULT_*``（见
          :func:`resolve_effective_memory_config`），**不**自动改写用户文件。
        - **WebUI 重置**：Runner 会生成无注释空壳；:meth:`on_load` 用 ``config.default.toml`` 覆盖。
        """
        normalized, changed = super().normalize_plugin_config(config_data)
        persistable = _dump_config_for_persist(normalized)
        return persistable, changed or persistable != normalized

    def get_components(self) -> list[dict[str, Any]]:
        """收集组件，并按配置覆盖注入 Hook 的超时时间。

        ``get_components()`` 在 Runner 应用完插件配置之后、组件注册之前被调用，
        因此可以在此处读取 ``hook_timeout_ms`` 并写回 Hook 组件元数据，
        让装饰器上的静态超时值变得可配置。
        """
        components = super().get_components()
        try:
            timeout_ms = resolve_effective_memory_config(self.config.memory).hook_timeout_ms
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
        effective = resolve_effective_memory_config(self.config.memory)
        self._size_limit = effective.size_limit
        self._per_chat_size_limit = effective.per_chat_size_limit
        self._per_chat_note_folder = self._resolve_chat_note_folder()
        self._inject_when_empty = effective.inject_when_empty
        self._inject_to_planner = effective.inject_to_planner
        self._inject_to_replyer = effective.inject_to_replyer
        self._max_compact_attempts = effective.max_compact_attempts
        self._compact_model = effective.compact_model
        self._compact_temperature = effective.compact_temperature
        self._compact_max_tokens = effective.compact_max_tokens
        self._hook_timeout_ms = effective.hook_timeout_ms
        self._injection_template = effective.injection_template
        self._replyer_injection_template = effective.replyer_injection_template
        self._stream_injection_section = effective.stream_injection_section
        self._compact_prompt_template = effective.compact_prompt_template
        self._note_file = effective.note_file
        self._chat_note_folder_name = effective.per_chat_note_folder

    def _resolve_path_under_plugin(self, raw: str, default: str) -> Path:
        """解析相对/绝对路径，相对路径基于插件目录。"""
        candidate = Path(raw or default).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (self._plugin_dir / candidate).resolve()

    def _resolve_note_path(self) -> Path:
        """解析全局笔记文件路径。"""
        return self._resolve_path_under_plugin(self._note_file, DEFAULT_NOTE_FILE)

    def _resolve_chat_note_folder(self) -> Path:
        """解析本聊天流便利贴目录路径。"""
        return self._resolve_path_under_plugin(self._chat_note_folder_name, DEFAULT_PER_CHAT_NOTE_FOLDER)

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
        return await self._inject(kwargs, self._injection_template)

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
        return await self._inject(kwargs, self._replyer_injection_template)

    async def _inject(self, kwargs: dict[str, Any], injection_template: str) -> dict[str, Any]:
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
                injection_template,
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
    # 工具：append_instruction / rewrite_instruction / compact_instructions
    # ------------------------------------------------------------------ #
    @Tool(
        "append_instruction",
        description=(
            "向自我演化接口写入演化指令。该接口每次请求都会注入在系统提示词之后，"
            "这是你唯一能反向改写自身的接口——写入即自我演化，请慎重。"
            "默认 scope=\"global\" 写入全局自我演化接口；scope=\"stream\" 仅写入当前聊天流的自我演化接口（其他聊天流看不到）。"
            "未指定 insert_after_string 时追加到演化指令末尾；"
            "指定时插入到该字符串【第一次出现】的位置之后（找不到则不写入）。"
            "建议使用 Markdown 书写（如标题、列表），但不强制。"
            "如果你的内容结尾没有换行，会自动补一个换行。"
            "注意：追加后若演化指令总字符数超过上限，会在后台异步触发一次 LLM 压缩重写（compact_instructions），"
            "压缩是异步的，本工具会立即返回。"
        ),
        parameters=[
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="要写入的演化指令正文。",
                required=True,
            ),
            SCOPE_TOOL_PARAM,
            ToolParameterInfo(
                name="insert_after_string",
                param_type=ToolParamType.STRING,
                description=(
                    "可选。若提供，则将 content 插入到此字符串第一次出现的位置之后；"
                    "省略则追加到演化指令末尾。"
                ),
                required=False,
            ),
        ],
    )
    async def append_instruction(
        self,
        content: str,
        scope: str = "global",
        insert_after_string: str = "",
        **kwargs: Any,
    ) -> dict[str, str]:
        content = _coalesce_text(content, kwargs, "instruction", "note", "text", "body", "markdown")
        if not str(content or "").strip():
            return {"content": "追加内容为空，未写入便利贴。请把正文放进 content 参数后重试（便利贴原内容保持不变）。"}
        store, size_limit, scope_label, error = self._resolve_target(scope, kwargs)
        if error:
            return {"content": error}

        async with store.lock:
            current = store.read()
            new_content, append_error, inserted_after_anchor = _apply_append_to_note(
                current,
                content,
                insert_after_string,
            )
            if append_error:
                return {"content": append_error}

            store.write(new_content)
            used = len(new_content)

        over = used > size_limit
        if over:
            self._schedule_compact(store, size_limit, scope_label)
        free = max(0, size_limit - used)
        if inserted_after_anchor:
            action = f"已在{scope_label}便利贴中指定字符串之后插入内容"
        else:
            action = f"已追加到{scope_label}便利贴末尾"
        message = f"{action}。当前 {used} 字符，剩余可用 {free} 字符（上限 {size_limit}）。"
        if over:
            message += " 笔记已超过上限，已在后台触发自动压缩。"
        return {"content": message}

    @Tool(
        "rewrite_instruction",
        description=(
            "用你提供的新内容【完全覆盖】演化指令——旧指令会被清空。自我演化接口每次请求都会注入在系统提示词之后，"
            "这是你唯一能反向改写自身的接口——写入即自我演化，请慎重。"
            "默认 scope=\"global\" 覆盖全局自我演化接口中的指令；scope=\"stream\" 仅覆盖当前聊天流的自我演化接口中的指令。"
            "建议使用 Markdown 书写，但不强制。"
            "注意：如果新指令的字符数超过上限，会在后台异步触发一次 LLM 压缩重写（compact_instructions），"
            "压缩是异步的，本工具会立即返回。"
        ),
        parameters=[
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="用于完全覆盖演化指令的全部新内容。",
                required=True,
            ),
            SCOPE_TOOL_PARAM,
        ],
    )
    async def rewrite_instruction(self, content: str, scope: str = "global", **kwargs: Any) -> dict[str, str]:
        content = _coalesce_text(content, kwargs, "instruction", "note", "text", "body", "markdown")
        if not str(content or "").strip():
            return {
                "content": (
                    "rewrite_instruction 收到空内容，已拒绝以防误清空演化指令（原内容已保留）。"
                    "请把完整新内容放进 content 参数后重试。"
                )
            }
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
        "compact_instructions",
        description=(
            "主动压缩演化指令，让它在不被你逐行重写的情况下变得更精炼。"
            "默认 scope=\"global\" 压缩全局自我演化接口中的指令；scope=\"stream\" 仅压缩当前聊天流自我演化接口中的指令。"
            "与超限时自动触发的压缩不同，本工具是【同步阻塞】的：它会等压缩完成，"
            "并返回压缩后的字符数与剩余可用字符数。"
            "若当前演化指令未超过上限，则不会改动内容。"
        ),
        parameters=[SCOPE_TOOL_PARAM],
    )
    async def compact_instructions(self, scope: str = "global", **kwargs: Any) -> dict[str, str]:
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
