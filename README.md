# ~~塑料内存条~~ 便利贴笔记 (Plastic Memory)

MaiBot 的记忆系统没法让麦麦随手写一段「便条」，并在之后每次思考、回复时一起带上。这个插件补的就是这块：给 LLM 一张**可以自己维护的便利贴**。

- **全局便利贴**（`my_memory.md`）：所有聊天流共享
- **本聊天流便利贴**（`chat_notes/<stream_id>.md`）：只在当前聊天流可见

> [!NOTE]
> 过了 81920 小时也不会自动清空。

---

## 能做什么

**自动注入**  
在 planner（规划）、时机判断、replyer（回复）请求里，插件会在系统提示词之后插入便利贴内容，并附带已用/上限等用量信息。

**三个维护工具**  
麦麦可以用 `append_note`、`rewrite_note`、`compact_notes` 自己管理笔记。三个工具都支持可选参数 `scope`：

- `global`（默认）→ 全局便利贴
- `stream` → 仅当前聊天流便利贴（stream id 由插件从上下文解析，**不能**指定其他聊天流）

**自动压缩**  
笔记超过字符上限时，会调用 LLM 按麦麦的人格与表达风格压缩重写。超限时 `append_note` / `rewrite_note` 会在后台异步触发压缩；也可以主动调用 `compact_notes` 同步等待结果。

> [!TIP]
> 笔记内容建议用 Markdown 书写（标题、列表等），工具提示里也会这样引导，但插件不强制校验格式。

> [!WARNING]
> 压缩默认走 `planner` 任务模型，可在配置里改成 `utils` 等。若 `size_limit` 设得很大，又用的是思考模型，请注意 token 消耗。

---

## 安装

1. 把本仓库放到 MaiBot 的 `plugins/` 目录，例如 `plugins/MaiBot-plastic-memory-plugin/`
2. 启动 MaiBot，插件会自动加载
3. 也可在 WebUI → 插件管理 里启用、改配置

除 `maibot-plugin-sdk` 外无其他第三方依赖。

---

## 工具说明

### 参数 `scope`

- `**scope="global"`**（默认）— 操作 `my_memory.md`
- `**scope="stream"`** — 操作当前聊天流的便利贴；stream id 由插件从上下文解析，工具不接受 stream id 参数，无法改动其他聊天流

> [!NOTE]
> 实现层还静默接受 `scope="chat"` 作为 `stream` 的别名，但 LLM 工具描述与注入提示里不会提到这一点。

### `append_note(content, scope="global", insert_after_string="")`

在便利贴中写入 `content`。若结尾没有换行会自动补上。超过上限时后台异步压缩，工具立即返回。

- 省略 **`insert_after_string`** — 追加到便利贴**末尾**（与原先行为相同）
- 指定 **`insert_after_string`** — 插入到该字符串**第一次出现**的位置之后；找不到则不写入

### `rewrite_note(content, scope="global")`

用 `content` **完全覆盖**便利贴。超过上限时同样在后台异步压缩。

### `compact_notes(scope="global")`

主动压缩。与超限自动压缩不同，本工具**同步阻塞**，会等压缩完成并返回当前字符数与剩余空间；未超限时不会改动内容。

---

## 配置（`config.toml`）

配置文件位于插件目录。也可在 WebUI 插件管理里编辑。下面按区块说明各字段含义与默认值。

### `[plugin]` — 插件开关

```toml
[plugin]
enabled = true
config_version = "1.2.0"
```

**`enabled`** — 是否启用插件  
**`config_version`** — 配置版本号

### `[memory]`

`[memory]` 下所有字段见下方完整示例；下文按用途分段说明。

```toml
[memory]
# ── 存储与上限 ──
size_limit = 8192
note_file = "my_memory.md"
per_chat_size_limit = 4096
per_chat_note_folder = "chat_notes"

# ── 注入 ──
inject_when_empty = true
inject_to_planner = true
inject_to_replyer = true
hook_timeout_ms = 60000
# 占位符：{nickname} {note} {used} {free} {size_limit} {stream_section}
# injection_template = """..."""          # planner / 时机判断
# replyer_injection_template = """..."""  # replyer（默认不提维护工具）
# 占位符：{stream_note} {stream_used} {stream_free} {stream_size_limit}
# stream_injection_section = """..."""

# ── 压缩 ──
max_compact_attempts = 3
compact_model = "planner"
compact_temperature = 0.3
compact_max_tokens = 0          # 0 = size_limit × 8
# 占位符：{nickname} {personality} {reply_style} {note_scope} {size_limit} {used} {note}
# compact_prompt_template = """..."""
```

#### 存储与上限

`**size_limit**` — 全局便利贴字符上限（按字符计，非字节）。默认 `8192`  
`**note_file**` — 全局便利贴路径；相对路径基于插件目录。默认 `my_memory.md`  
`**per_chat_size_limit**` — 本聊天流便利贴字符上限。默认 `4096`  
`**per_chat_note_folder**` — 本聊天流目录；每流一个 `<stream_id>.md`。默认 `chat_notes`

#### 注入

`**inject_when_empty**` — 空笔记时是否仍注入提示。默认 `true`  
`**inject_to_planner**` / `**inject_to_replyer**` — 是否在 planner / 时机判断 / replyer 中注入。默认均为 `true`  
`**hook_timeout_ms**` — 注入 Hook 处理器超时（毫秒）。默认 `60000`（**不是**压缩 LLM 的超时）  
**`injection_template`** — planner / 时机判断注入模板；含便利贴维护工具说明。默认把含 `{nickname}` 的说明放在【全局便利贴】之前，便于模型缓存  
**`replyer_injection_template`** — replyer 专用注入模板；默认仅提示参考先前备忘，不提及维护工具或二级指令定位（replyer 无工具调用能力）  
**`stream_injection_section`** — 本聊天流便利贴子模板，渲染后填入 `{stream_section}`

模板字符串较长，完整默认值见仓库内 `config.toml`。

> [!NOTE]
> 当前 `ctx.llm.generate` 未暴露单次 LLM 超时参数，插件无法单独配置压缩调用的超时。

#### 压缩

`**max_compact_attempts**` — 单次压缩仍超限时，最多递归重试次数。默认 `3`  
`**compact_model**` — 压缩使用的 LLM 任务名。默认 `planner`  
`**compact_temperature**` — 压缩采样温度。默认 `0.3`  
`**compact_max_tokens**` — 压缩 `max_tokens`；`0` 表示按对应 `size_limit × 8` 自动计算。默认 `0`  
`**compact_prompt_template**` — 压缩时发给 LLM 的提示词模板

---

## 工作原理

注入靠两个 Hook：

- `maisaka.planner.before_request` — planner 主流程与时机判断
- `maisaka.replyer.before_model_request` — replyer

插件读取全局与本聊天流便利贴，合成一条 `user` 消息，插在最后一条 `system` 消息之后。聊天流 id 从 Hook 上下文里的 `session_id` 解析。

压缩时会读取 `bot.nickname`、`personality.personality`、`personality.reply_style`，让 LLM 以麦麦的身份重写笔记。

### 压缩时的 `max_tokens` 与重试

```toml
compact_max_tokens = 0   # 默认：初始 max_tokens = size_limit × 8
```

- `**compact_max_tokens = 0**` — 为推理模型的 reasoning token 留余量，自动按 `size_limit × 8` 计算
- `**compact_max_tokens > 0**` — 使用固定值；若小于 `size_limit`，日志会告警

**长度触顶重试** — 每次压缩后判断是否可能被截断：

- 优先看 Host 返回的 `finish_reason`
- 若无该字段：返回字符数 **< `size_limit` 的 80%** 时视为可能被截断

需要重试时，临时将 `max_tokens` 翻倍，最多重试 **3** 次（加首次共 **4** 次调用），最终取**最长**的那次响应写入。

---

## 常见问题

> **便利贴存在哪？**

- 全局：`my_memory.md`（可用 `note_file` 改路径）
- 本聊天流：`chat_notes/` 下，每个聊天流一个文件

以上文件已在 `.gitignore` 中，不会进版本库。

> **全局和本聊天流便利贴有什么区别？**

全局的在所有聊天里都会注入；本聊天流的只在对应聊天流里出现，适合记「这个群/这个私聊」相关的备忘。

> **为什么 8192 字符的文件更大？**

`size_limit` 按**字符**计。中文 UTF-8 编码下每字约 3 字节，文件字节数大于字符数是正常现象。

> **压缩会死循环吗？**

不会。有 `max_compact_attempts` 上限；多次仍超限也会写入目前最短的结果后停止。

---

## 许可证

MIT