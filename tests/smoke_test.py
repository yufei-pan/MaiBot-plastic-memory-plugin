"""离线冒烟测试：不依赖 MaiBot Host，验证配置迁移与解析逻辑。

运行方式（在插件目录）：
    PYTHONPATH=../maibot-plugin-sdk python tests/smoke_test.py
"""

from __future__ import annotations

import sys
import tomllib
from copy import deepcopy
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

import plugin as plastic_plugin  # noqa: E402


def test_migrate_legacy_baked_defaults() -> None:
    legacy = {
        "plugin": {"enabled": True, "config_version": "1.2.0"},
        "memory": {
            "size_limit": 8192,
            "note_file": "my_memory.md",
            "per_chat_size_limit": 4096,
            "per_chat_note_folder": "chat_notes",
            "inject_when_empty": True,
            "inject_to_planner": True,
            "inject_to_replyer": True,
            "max_compact_attempts": 3,
            "compact_model": "planner",
            "compact_temperature": 0.3,
            "compact_max_tokens": 0,
            "hook_timeout_ms": 60000,
            "injection_template": plastic_plugin.DEFAULT_INJECTION_TEMPLATE,
            "replyer_injection_template": plastic_plugin.DEFAULT_REPLYER_INJECTION_TEMPLATE,
            "stream_injection_section": plastic_plugin.DEFAULT_STREAM_INJECTION_SECTION,
            "compact_prompt_template": plastic_plugin.DEFAULT_COMPACT_PROMPT_TEMPLATE,
        },
    }
    migrated, changed = plastic_plugin._migrate_legacy_baked_defaults(deepcopy(legacy))
    assert changed
    assert migrated["plugin"]["config_version"] == plastic_plugin.CURRENT_CONFIG_VERSION
    assert migrated["memory"]["size_limit"] is None
    assert migrated["memory"]["note_file"] == ""
    assert migrated["memory"]["injection_template"] == ""

    custom = deepcopy(legacy)
    custom["memory"]["size_limit"] = 12000
    migrated_custom, custom_changed = plastic_plugin._migrate_legacy_baked_defaults(custom)
    assert custom_changed
    assert migrated_custom["memory"]["size_limit"] == 12000
    print("ok: legacy baked defaults migrated with custom size_limit preserved")


def test_resolve_effective_defaults() -> None:
    cfg = plastic_plugin.PlasticMemoryConfig()
    eff = plastic_plugin.resolve_effective_memory_config(cfg.memory)
    assert eff.size_limit == 8192
    assert eff.note_file == "my_memory.md"
    assert eff.hook_timeout_ms == 60000
    assert eff.injection_template == plastic_plugin.DEFAULT_INJECTION_TEMPLATE
    print("ok: resolve effective defaults")


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


def main() -> None:
    test_plugin_importable()
    test_resolve_effective_defaults()
    test_migrate_legacy_baked_defaults()
    print("\n全部冒烟测试通过")


if __name__ == "__main__":
    main()
