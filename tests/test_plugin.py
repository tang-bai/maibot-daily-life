"""SDK loading/authorization/config tests; no external services are contacted."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import plugin
from daily_life.config import Settings
from daily_life.content import ContentError, parse_content
from daily_life.publisher import PublishError, PublishUncertain, receipt

ROOT = Path(__file__).resolve().parents[1]


class ProjectTests(unittest.TestCase):
    def test_sdk_components_and_safe_defaults(self):
        instance = plugin.create_plugin()
        defaults = instance.get_default_config()
        self.assertFalse(defaults["trigger"]["auto_enabled"])
        self.assertEqual(defaults["publish"]["visibility"], 4)
        self.assertEqual(defaults["trigger"]["daily_limit"], 3)
        components = instance.get_components()
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["type"], "COMMAND")
        pattern = components[0]["metadata"]["command_pattern"]
        for text in ("/daily", "/daily status", "/daily preview", "/daily post", "/daily publish " + "a" * 32, "/daily retry " + "b" * 32):
            self.assertIsNotNone(re.fullmatch(pattern, text))
        self.assertIsNone(re.fullmatch(pattern, "/daily preview malicious"))
        self.assertEqual(len(instance.get_webui_config_schema()["sections"]), 7)

    def test_webui_sections_and_all_field_labels_are_chinese(self):
        schema = plugin.create_plugin().get_webui_config_schema()
        expected_titles = {
            "plugin": "基本设置", "character": "角色与画风", "text": "配文生成",
            "image": "绘图服务", "trigger": "自动发布规则", "publish": "空间发布",
            "storage": "缓存与记录",
        }
        defaults = Settings().model_dump()
        self.assertEqual(set(schema["sections"]), set(expected_titles))
        field_count = 0
        for key, title in expected_titles.items():
            section = schema["sections"][key]
            self.assertEqual(section["title"], title)
            self.assertRegex(section["description"], r"[\u4e00-\u9fff]")
            self.assertEqual(section["name"], key)
            self.assertEqual(set(section["fields"]), set(defaults[key]))
            for name, field in section["fields"].items():
                with self.subTest(section=key, field=name):
                    self.assertRegex(field["label"], r"[\u4e00-\u9fff]")
                    self.assertRegex(field["description"], r"[\u4e00-\u9fff]")
                    self.assertNotEqual(field["label"], name)
                    self.assertEqual(field["name"], name)
                    self.assertEqual(field["default"], defaults[key][name])
                field_count += 1
        self.assertEqual(field_count, 27)
        image_fields = schema["sections"]["image"]["fields"]
        self.assertIn("直接填写密钥", image_fields["api_key"]["hint"])
        self.assertEqual(image_fields["api_key"]["input_type"], "password")
        self.assertEqual(image_fields["api_key"]["default"], "")
        self.assertNotIn("api_key_env", image_fields)
        self.assertEqual(schema["sections"]["publish"]["fields"]["visibility"]["choices"], [1, 4, 64])

    def test_api_key_persists_in_config_but_not_repr_and_legacy_field_is_ignored(self):
        settings = Settings.model_validate({"image": {"api_key": "synthetic-config-secret", "api_key_env": "OLD_VARIABLE"}})
        self.assertEqual(settings.image.api_key, "synthetic-config-secret")
        self.assertEqual(settings.model_dump()["image"]["api_key"], "synthetic-config-secret")
        self.assertNotIn("synthetic-config-secret", repr(settings))
        self.assertNotIn("api_key_env", settings.model_dump()["image"])
        legacy = Settings.model_validate({"image": {"api_key_env": "synthetic-old-pasted-secret"}})
        self.assertEqual(legacy.image.api_key, "")
        instance = plugin.create_plugin()
        instance.set_plugin_config(settings.model_dump())
        self.assertEqual(instance.config.image.api_key, "synthetic-config-secret")
        self.assertEqual(instance.get_webui_config_schema()["sections"]["image"]["fields"]["api_key"]["default"], "")

    def test_example_toml_exactly_matches_defaults(self):
        data = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf8"))
        self.assertEqual(Settings.model_validate(data).model_dump(), Settings().model_dump())

    def test_manifest_dependencies_and_capabilities(self):
        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf8"))
        self.assertEqual(manifest["sdk"]["min_version"], "2.21.0")
        self.assertEqual(set(manifest["capabilities"]), {"api.call", "api.get", "llm.generate", "send.text", "send.image"})
        self.assertEqual(manifest["host_application"]["max_version"], "1.3.99")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf8"))
        self.assertEqual(manifest["version"], project["project"]["version"])
        self.assertTrue(manifest["author"]["url"].startswith("https://"))
        self.assertTrue(manifest["urls"]["repository"].startswith("https://"))

    def test_manifest_follows_marketplace_conventions(self):
        import re

        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf8"))
        # Undefined manifest keys make Host validation fail.
        self.assertLessEqual(set(manifest), {
            "manifest_version", "plugin_type", "id", "version", "name", "description", "author",
            "license", "urls", "host_application", "sdk", "dependencies", "llm_providers",
            "capabilities", "i18n", "display", "changelog",
        })
        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["plugin_type"], "automation")
        # Marketplace guidance: github.<owner>.<plugin>; Host also accepts the
        # shorter owner.plugin form, so the shared pattern allows both.
        self.assertRegex(manifest["id"], r"^[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)+$")
        self.assertTrue(manifest["id"].startswith("github."))
        self.assertEqual(manifest["id"], manifest["id"].casefold())
        # Versions must be strict three-part, without 99/999 in major or minor.
        for value in (manifest["version"], manifest["host_application"]["min_version"],
                      manifest["host_application"]["max_version"]):
            self.assertRegex(value, r"^\d+\.\d+\.\d+$")
            major, minor, _patch = value.split(".")
            self.assertNotIn(major, {"99", "999"})
            self.assertNotIn(minor, {"99", "999"})
        self.assertLessEqual(manifest["host_application"]["min_version"],
                             manifest["host_application"]["max_version"])
        for key in ("repository", "homepage", "documentation", "issues"):
            self.assertTrue(manifest["urls"][key].startswith("https://github.com/"), key)
        self.assertTrue(manifest["author"]["url"].startswith("https://github.com/"))
        # Required marketplace files, and no runtime config committed at root.
        for name in ("plugin.py", "LICENSE", "README.md", "_manifest.json"):
            self.assertTrue((ROOT / name).is_file(), name)
        self.assertFalse((ROOT / "config.toml").exists())
        self.assertIn("config.toml", (ROOT / ".gitignore").read_text(encoding="utf8"))
        self.assertIn(manifest["license"], (ROOT / "LICENSE").read_text(encoding="utf8"))
        self.assertTrue(re.search(r"[\u4e00-\u9fff]", (ROOT / "README.md").read_text(encoding="utf8")))

    def test_release_refuses_to_package_a_populated_example_key(self):
        from scripts.build_release import build
        from unittest.mock import patch

        with patch("scripts.build_release.tomllib.loads", return_value={"image": {"api_key": "synthetic-secret"}}):
            with self.assertRaisesRegex(RuntimeError, "must be empty"):
                build()

    def test_release_preflight_rejects_missing_installer_metadata(self):
        from copy import deepcopy
        from scripts.build_release import validate_manifest_metadata

        manifest = json.loads((ROOT / "_manifest.json").read_text(encoding="utf8"))
        validate_manifest_metadata(manifest)
        for field in ("author.url", "urls", "urls.repository"):
            broken = deepcopy(manifest)
            parent = broken
            parts = field.split(".")
            for part in parts[:-1]:
                parent = parent[part]
            del parent[parts[-1]]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Missing required manifest field"):
                validate_manifest_metadata(broken)
        for field in ("author", "urls"):
            broken = deepcopy(manifest)
            broken[field]["url" if field == "author" else "repository"] = "not-a-url"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "Manifest URL"):
                validate_manifest_metadata(broken)

    def test_runner_synthetic_package_import_outside_workspace(self):
        name = "_test_daily_runner"
        spec = importlib.util.spec_from_file_location(name, ROOT / "plugin.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        old = Path.cwd()
        try:
            with tempfile.TemporaryDirectory() as folder:
                os.chdir(folder)
                try:
                    spec.loader.exec_module(module)
                    self.assertEqual(len(module.create_plugin().get_components()), 1)
                    self.assertEqual(module.Settings.__module__, name + ".daily_life.config")
                finally:
                    os.chdir(old)
        finally:
            os.chdir(old)
            for key in list(sys.modules):
                if key == name or key.startswith(name + "."):
                    del sys.modules[key]

    def test_configuration_rejects_invalid_values(self):
        for data in ({"publish": {"visibility": 16}}, {"trigger": {"timezone": "Not/AZone"}},
                     {"trigger": {"daily_limit": 0}}, {"plugin": {"admin_qqs": ["user"]}},
                     {"image": {"timeout_seconds": 0}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                Settings.model_validate(data)

    def test_content_json_validation(self):
        caption = "捧着书坐下来慢慢读几页，让今天的脚步也跟着放轻一点，这样安静的小片刻真舒服。"
        obj = {"caption": caption, "image_prompt": "An adult character reading a book indoors in warm light."}
        self.assertEqual(parse_content({"success": True, "response": json.dumps(obj)}), (caption, obj["image_prompt"]))
        fenced = chr(96) * 3 + "json\n" + json.dumps(obj) + "\n" + chr(96) * 3
        self.assertEqual(parse_content({"success": True, "response": fenced})[0], caption)
        for raw in ("not json", "[]", "{}", json.dumps({**obj, "caption": "太短"}),
                    json.dumps({**obj, "image_prompt": "https://secret.example/a"}),
                    json.dumps({**obj, "caption": caption + "123456789"})):
            with self.subTest(raw=raw), self.assertRaises(ContentError):
                parse_content({"success": True, "response": raw})
        with self.assertRaises(ContentError):
            parse_content({"success": False, "response": json.dumps(obj)})

    def test_receipt_requires_success_and_tid(self):
        self.assertEqual(receipt({"status": "ok", "retcode": 0, "data": {"tid": "abc"}}), "abc")
        for response in (None, {}, {"tid": "abc"}, {"status": "ok", "retcode": 0, "data": {}},
                         {"status": "ok", "retcode": False, "data": {"tid": "abc"}},
                         {"status": "ok", "retcode": 0, "data": {"tid": 123}}):
            with self.subTest(response=response), self.assertRaises(PublishUncertain):
                receipt(response)
        with self.assertRaises(PublishError) as cm:
            receipt({"status": "failed", "retcode": 100, "message": "SECRET"})
        self.assertNotIn("SECRET", str(cm.exception))


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        instance = plugin.create_plugin()
        settings = Settings()
        settings.plugin.admin_qqs = ["12345"]
        service = SimpleNamespace(settings=settings, preview=AsyncMock(), post=AsyncMock(), retry=AsyncMock(), publish_draft=AsyncMock(),
                                  status=AsyncMock(return_value="status"), notify=AsyncMock())
        service.spawn = Mock(side_effect=lambda coro, **kw: coro.close())
        instance.service = service
        return instance, service

    async def test_authorization_uses_host_identity_not_command_fields(self):
        instance, service = self.make_plugin()
        for platform, uid in (("qq", "999"), ("discord", "12345"), ("", "12345"), ("qq", "")):
            for text in ("/daily preview", "/daily post", "/daily retry " + "a" * 32):
                result = await instance.handle_daily(text=text, stream_id="s", platform=platform, user_id=uid,
                                                     matched_groups={"user_id": "12345"})
                self.assertFalse(result[0])
        service.spawn.assert_not_called()

    async def test_admin_status_preview_and_publish_dispatch(self):
        instance, service = self.make_plugin()
        for text in ("/daily", "/daily status", "/daily preview", "/daily post", "/daily publish " + "a" * 32, "/daily retry " + "b" * 32):
            result = await instance.handle_daily(text=text, stream_id="s", platform="qq", user_id="12345")
            self.assertTrue(result[0], text)
        self.assertEqual(service.spawn.call_count, 6)
        service.preview.assert_called_once_with("s")
        service.post.assert_called_once_with("s")
        service.retry.assert_called_once_with("b" * 32, "s")
        service.publish_draft.assert_called_once_with("a" * 32, "s")
        self.assertEqual(service.spawn.call_args_list[0].kwargs, {"busy": False})

    async def test_invalid_commands_cannot_start_work(self):
        instance, service = self.make_plugin()
        for text in ("/daily publish", "/daily status " + "a" * 32, "/daily preview " + "a" * 32,
                     "/daily publish ../../file", "/daily post " + "a" * 32, "/daily post extra", "/daily retry", "/daily retry bad", "/daily anything"):
            result = await instance.handle_daily(text=text, stream_id="s", platform="qq", user_id="12345")
            self.assertFalse(result[0], text)
        service.spawn.assert_not_called()

    async def test_disabled_plugin_cannot_generate_or_publish(self):
        instance, service = self.make_plugin()
        service.settings.plugin.enabled = False
        for text in ("/daily preview", "/daily post", "/daily publish " + "a" * 32, "/daily retry " + "a" * 32):
            self.assertFalse((await instance.handle_daily(text=text, stream_id="s", platform="qq", user_id="12345"))[0])
        service.spawn.assert_not_called()

    async def test_load_reload_self_and_unload_cancel_loop(self):
        instance = plugin.create_plugin()
        instance.set_plugin_config(Settings().model_dump())
        with tempfile.TemporaryDirectory() as folder:
            ctx = SimpleNamespace(paths=SimpleNamespace(data_dir=Path(folder)), logger=logging.getLogger("tests.plugin"))
            instance._set_context(ctx)
            await instance.on_load()
            old_service = instance.service
            old_task = instance.poll_task
            await asyncio.sleep(0)
            await instance.on_config_update("model", {}, "x")
            self.assertIs(instance.service, old_service)
            settings = Settings()
            settings.publish.visibility = 64
            settings.image.api_key = "synthetic-hot-reloaded-secret"
            instance.set_plugin_config(settings.model_dump())
            await instance.on_config_update("self", settings.model_dump(), "x")
            self.assertTrue(old_service.closed)
            self.assertTrue(old_task.done())
            self.assertIsNot(instance.service, old_service)
            self.assertEqual(instance.service.settings.publish.visibility, 64)
            self.assertEqual(instance.service.images.settings.api_key, "synthetic-hot-reloaded-secret")
            self.assertEqual(old_service.settings.image.api_key, "")
            await instance.on_unload()
            self.assertIsNone(instance.service)
            self.assertIsNone(instance.poll_task)


if __name__ == "__main__":
    unittest.main()
