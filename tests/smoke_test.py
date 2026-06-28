"""离线冒烟测试：不依赖 MaiBot Host，验证配置迁移与解析逻辑。

运行方式（在插件目录）：
    PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
"""

from __future__ import annotations

import subprocess
import sys
import shutil
import tomllib
from copy import deepcopy
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import tomlkit  # noqa: E402

import plugin as plastic_plugin  # noqa: E402
from maibot_sdk.config import rebuild_plugin_config_data  # noqa: E402


def _flatten_values(value: object) -> list[object]:
    if isinstance(value, dict):
        items: list[object] = []
        for nested in value.values():
            items.extend(_flatten_values(nested))
        return items
    return [value]


def _legacy_1_2_0_config() -> dict:
    legacy_toml = subprocess.check_output(
        ["git", "show", "d993b51:config.toml"],
        cwd=PLUGIN_DIR,
        text=True,
    )
    return tomllib.loads(legacy_toml)


def _has_extra_config_keys(existing_config: object, latest_config: object) -> bool:
    if not isinstance(existing_config, dict) or not isinstance(latest_config, dict):
        return False
    for key, existing_value in existing_config.items():
        if key not in latest_config:
            return True
        if _has_extra_config_keys(existing_value, latest_config[key]):
            return True
    return False


def test_legacy_runtime_default_follows_code_not_file() -> None:
    """文件里仍是 1.2.0 写死默认时，运行时跟随当前代码默认（不改写磁盘）。"""
    memory = plastic_plugin.MemorySectionConfig(size_limit=8192)
    assert plastic_plugin.resolve_effective_memory_config(memory).size_limit == 8192

    assert plastic_plugin._effective_int(8192, 16384, legacy=8192, minimum=1) == 16384
    assert plastic_plugin._effective_int(12000, 16384, legacy=8192, minimum=1) == 12000
    print("ok: legacy baked runtime values follow current code default")


def test_restore_shipped_config_template() -> None:
    """Runner 生成的无注释空壳应被 config.default.toml 覆盖。"""
    import tempfile

    template_src = PLUGIN_DIR / "config.default.toml"
    assert template_src.exists()

    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / "config.toml").write_text(
            '[plugin]\nenabled = true\nconfig_version = "1.4.0"\n',
            encoding="utf-8",
        )
        shutil.copy2(template_src, plugin_dir / plastic_plugin.SHIPPED_CONFIG_TEMPLATE_NAME)
        assert plastic_plugin._restore_shipped_config_template(plugin_dir)

        restored_text = (plugin_dir / "config.toml").read_text(encoding="utf-8")
        assert "# 塑料内存条" in restored_text
        assert "# size_limit = 8192" in restored_text
        assert plastic_plugin._is_runner_generated_bare_config(plugin_dir / "config.toml") is False
    print("ok: bare config restored from shipped template")


def test_version_upgrade_preserves_user_fields() -> None:
    """1.2.0 -> 1.3.0 升级应保留用户显式配置，仅 bump config_version。"""
    raw = _legacy_1_2_0_config()
    default = plastic_plugin.PlasticMemoryPlugin.build_default_config()
    rebuilt = rebuild_plugin_config_data(default, raw)
    inst = plastic_plugin.create_plugin()
    normalized, _ = inst.normalize_plugin_config(rebuilt)

    assert normalized["plugin"]["config_version"] == plastic_plugin.CURRENT_CONFIG_VERSION
    assert normalized["memory"]["size_limit"] == 8192
    assert normalized["memory"]["hook_timeout_ms"] == 60000
    effective = plastic_plugin.resolve_effective_memory_config(
        plastic_plugin.MemorySectionConfig(**normalized["memory"])
    )
    assert effective.injection_template == plastic_plugin.DEFAULT_INJECTION_TEMPLATE
    assert effective.replyer_injection_template == plastic_plugin.DEFAULT_REPLYER_INJECTION_TEMPLATE
    assert effective.stream_injection_section == plastic_plugin.DEFAULT_STREAM_INJECTION_SECTION
    assert all(value is not None for value in _flatten_values(normalized))
    print("ok: version upgrade keeps explicit user fields")


def test_runner_save_path_preserves_content() -> None:
    """模拟 Runner 落盘：升级后配置可 tomlkit 序列化，且不删光 memory 字段。"""
    raw = _legacy_1_2_0_config()
    default = plastic_plugin.PlasticMemoryPlugin.build_default_config()
    rebuilt = rebuild_plugin_config_data(default, raw)
    inst = plastic_plugin.create_plugin()
    normalized, _ = inst.normalize_plugin_config(rebuilt)

    assert normalized["memory"]["size_limit"] == 8192
    tomlkit.dumps(normalized)

    legacy_toml = subprocess.check_output(
        ["git", "show", "d993b51:config.toml"],
        cwd=PLUGIN_DIR,
        text=True,
    )
    existing_document = tomlkit.loads(legacy_toml)
    existing_config = existing_document.unwrap()

    # 键未大量删除时不应触发「删键整文件重写」路径
    assert not _has_extra_config_keys(existing_config, normalized)

    for key, value in normalized.items():
        if isinstance(value, dict) and key in existing_document:
            for field, field_value in value.items():
                existing_document[key][field] = tomlkit.item(field_value)
        else:
            existing_document[key] = tomlkit.item(value)
    dumped = tomlkit.dumps(existing_document)
    assert "size_limit = 8192" in dumped
    assert 'config_version = "1.4.0"' in dumped
    print("ok: runner merge path keeps memory fields and comments")


def test_none_values_never_persisted() -> None:
    """模型默认 None 不得出现在落盘字典中（tomlkit 无法序列化）。"""
    inst = plastic_plugin.create_plugin()
    normalized, _ = inst.normalize_plugin_config({})
    assert all(value is not None for value in _flatten_values(normalized))
    tomlkit.dumps(normalized)
    print("ok: empty input normalizes without None for persist")


def test_resolve_effective_defaults() -> None:
    cfg = plastic_plugin.PlasticMemoryConfig()
    eff = plastic_plugin.resolve_effective_memory_config(cfg.memory)
    assert eff.size_limit == 8192
    assert eff.note_file == "my_memory.md"
    assert eff.hook_timeout_ms == 60000
    assert eff.compact_model == "replyer"
    assert eff.rewrite_model == "replyer"
    assert eff.llm_rewrite_writes is True
    assert eff.max_rewrite_attempts == 3
    assert eff.injection_template == plastic_plugin.DEFAULT_INJECTION_TEMPLATE
    print("ok: resolve effective defaults")


def test_legacy_compact_model_follows_replyer() -> None:
    memory = plastic_plugin.MemorySectionConfig(compact_model="planner")
    eff = plastic_plugin.resolve_effective_memory_config(memory)
    assert eff.compact_model == "replyer"
    print("ok: legacy compact_model planner follows replyer")


def test_resolve_rewrite_max_tokens_uses_size_limit_multiplier() -> None:
    assert plastic_plugin._resolve_rewrite_max_tokens(0, 8192) == 8192 * 8
    assert plastic_plugin._resolve_rewrite_max_tokens(0, 4096) == 4096 * 8
    assert plastic_plugin._resolve_rewrite_max_tokens(12000, 8192) == 12000
    print("ok: rewrite max_tokens follows size_limit multiplier")


def test_build_rewrite_payload_excludes_host_context() -> None:
    payload = plastic_plugin._build_rewrite_payload(
        "正文",
        {
            "scope": "global",
            "insert_after_string": "锚点",
            "stream_id": "stream-1",
            "chat_id": "stream-1",
            "group_id": "g1",
            "user_id": "u1",
            "platform": "qq",
            "reasoning": "思考过程",
            "action_data": {"k": "v"},
        },
    )
    assert "scope:" not in payload
    assert "insert_after_string:" not in payload
    assert "stream_id:" not in payload
    assert "chat_id:" not in payload
    assert "group_id:" not in payload
    assert "content: 正文" in payload
    print("ok: rewrite payload excludes host context")


def test_empty_content_with_host_context_skips_llm() -> None:
    """无实质正文时，即使 Host 注入 stream_id 等 kwargs 也不得触发后台整理。"""
    p = plastic_plugin.PlasticMemoryPlugin()
    p._llm_rewrite_writes = True

    content, payload, notice = p._prepare_instruction_write(
        "",
        {"stream_id": "test-stream", "chat_id": "test-stream"},
    )
    assert content == ""
    assert payload == ""
    assert notice == ""
    print("ok: empty content with host context skips llm")


def test_prepare_instruction_write_schedules_async_notice() -> None:
    p = plastic_plugin.PlasticMemoryPlugin()
    p._llm_rewrite_writes = True
    content, payload, notice = p._prepare_instruction_write("原始正文", {"scope": "global"})
    assert content == "原始正文"
    assert "原始正文" in payload
    assert "后台" in notice
    print("ok: prepare instruction write returns async notice")


def test_default_prompt_templates_recommend_markdown() -> None:
    assert "Markdown" in plastic_plugin.DEFAULT_REWRITE_PROMPT_TEMPLATE
    assert "Markdown" in plastic_plugin.DEFAULT_COMPACT_PROMPT_TEMPLATE
    assert "Markdown 代码块包裹" not in plastic_plugin.DEFAULT_REWRITE_PROMPT_TEMPLATE
    print("ok: default prompt templates recommend markdown")


def test_build_rewrite_payload_excludes_scope_and_anchor() -> None:
    payload = plastic_plugin._build_rewrite_payload(
        "",
        {
            "scope": "global",
            "insert_after_string": "锚点",
            "text": "正文",
            "note": "忽略",
        },
    )
    assert "scope:" not in payload
    assert "insert_after_string:" not in payload
    assert "text: 正文" in payload
    print("ok: rewrite payload excludes structural args")


def test_plugin_importable() -> None:
    instance = plastic_plugin.create_plugin()
    assert instance is not None
    default_config = type(instance).build_default_config()
    assert default_config["plugin"]["config_version"] == plastic_plugin.CURRENT_CONFIG_VERSION
    assert default_config["memory"]["size_limit"] is None
    assert default_config["memory"]["injection_template"] == ""

    config_data = tomllib.loads((PLUGIN_DIR / "config.toml").read_text(encoding="utf-8"))
    for section, fields in config_data.items():
        assert section in default_config, f"config.toml 中存在未知配置节：{section}"
        for field in fields:
            assert field in default_config[section], f"config.toml 中存在未知字段：{section}.{field}"
    print("ok: plugin importable, config model consistent")


def test_custom_size_limit_preserved_on_upgrade() -> None:
    raw = _legacy_1_2_0_config()
    raw["memory"]["size_limit"] = 12000
    default = plastic_plugin.PlasticMemoryPlugin.build_default_config()
    rebuilt = rebuild_plugin_config_data(default, raw)
    inst = plastic_plugin.create_plugin()
    normalized, _ = inst.normalize_plugin_config(rebuilt)
    assert normalized["memory"]["size_limit"] == 12000
    print("ok: customized size_limit preserved on upgrade")


def test_inject_continues_when_one_scope_read_fails() -> None:
    """某一作用域读取失败时，仍注入另一作用域已成功读取的内容。"""
    import asyncio
    import tempfile
    from unittest.mock import AsyncMock, MagicMock

    p = plastic_plugin.PlasticMemoryPlugin()
    tmp = Path(tempfile.mkdtemp(prefix="pm-smoke-"))
    stream_store = plastic_plugin.NoteStore(tmp / "chat_notes" / "stream-1.md")
    stream_store.write("聊天流演化指令\n")
    p._inject_when_empty = False
    p._size_limit = 8192
    p._global_store = plastic_plugin.NoteStore(tmp / "global.md")

    class FailingGlobalStore:
        def read(self) -> str:
            raise plastic_plugin.NoteStoreReadError("全局读取失败")

    p._global_store = FailingGlobalStore()
    p._get_chat_store = lambda _stream_id: stream_store
    p._set_context(
        MagicMock(
            config=MagicMock(get=AsyncMock(side_effect=lambda key, default=None: "麦麦")),
            logger=MagicMock(),
        )
    )

    kwargs: dict = {
        "messages": [{"role": "system", "content": "system prompt"}],
        "stream_id": "stream-1",
    }
    result = asyncio.run(p._inject(kwargs, p._injection_template))
    assert "modified_kwargs" in result
    injected = result["modified_kwargs"]["messages"][1]["content"]
    assert "聊天流演化指令" in injected
    assert "自我演化接口" in injected
    print("ok: inject continues with partial read success")


def test_compact_restores_when_file_cleared_during_llm() -> None:
    """压缩期间文件被清空时，应写入 LLM 压缩结果而非静默丢弃。"""
    import asyncio
    import tempfile
    from unittest.mock import MagicMock

    p = plastic_plugin.PlasticMemoryPlugin()
    tmp = Path(tempfile.mkdtemp(prefix="pm-smoke-"))
    store = plastic_plugin.NoteStore(tmp / "note.md")
    store.write("x" * 100)
    p._set_context(MagicMock(logger=MagicMock()))

    async def fake_compact_and_clear(_content: str, _size_limit: int, _scope_label: str) -> str:
        store.path.unlink(missing_ok=True)
        return "compressed\n"

    p._compact_content_with_llm = fake_compact_and_clear

    result_len = asyncio.run(p._compact(store, 50, "全局"))
    assert store.read() == "compressed\n"
    assert result_len == len("compressed\n")
    print("ok: compact restores when file cleared during llm")


def test_instruction_write_feedback() -> None:
    """演化指令写入工具：空内容不得静默写入/清空，且容忍 instruction/note/text 别名。"""
    import asyncio
    import tempfile

    p = plastic_plugin.PlasticMemoryPlugin()
    p._llm_rewrite_writes = False
    tmp = Path(tempfile.mkdtemp(prefix="pm-smoke-"))
    p._global_store = plastic_plugin.NoteStore(tmp / "note.md")
    p._size_limit = 100000
    p._global_store.write("原有重要内容\n")

    # 空 rewrite 必须被拒绝且不清空
    asyncio.run(p.rewrite_instruction(content="", scope="global"))
    assert p._global_store.read().strip() == "原有重要内容", "空 rewrite 不得清空演化指令"
    # instruction 别名应正确覆盖
    asyncio.run(p.rewrite_instruction(content="", scope="global", instruction="新演化指令内容"))
    assert "新演化指令内容" in p._global_store.read()
    # 空 append 必须被拒绝
    before = p._global_store.read()
    asyncio.run(p.append_instruction(content="", scope="global"))
    assert p._global_store.read() == before, "空 append 不应改动演化指令"
    # text 别名应生效
    asyncio.run(p.append_instruction(content="", scope="global", text="追加的一段"))
    assert "追加的一段" in p._global_store.read()


def main() -> None:
    test_plugin_importable()
    test_instruction_write_feedback()
    test_resolve_effective_defaults()
    test_legacy_compact_model_follows_replyer()
    test_resolve_rewrite_max_tokens_uses_size_limit_multiplier()
    test_build_rewrite_payload_excludes_host_context()
    test_empty_content_with_host_context_skips_llm()
    test_prepare_instruction_write_schedules_async_notice()
    test_default_prompt_templates_recommend_markdown()
    test_build_rewrite_payload_excludes_scope_and_anchor()
    test_inject_continues_when_one_scope_read_fails()
    test_compact_restores_when_file_cleared_during_llm()
    test_legacy_runtime_default_follows_code_not_file()
    test_restore_shipped_config_template()
    test_version_upgrade_preserves_user_fields()
    test_runner_save_path_preserves_content()
    test_none_values_never_persisted()
    test_custom_size_limit_preserved_on_upgrade()
    print("\n全部冒烟测试通过")


if __name__ == "__main__":
    main()
