"""Installing, checking and removing the Presto Claude mod.

The one thing the collector does to Claude Code's own files, so the tests hold
it to its promises: nothing changes until the user agrees, the settings file is
copied before it is touched, a file it cannot safely change is left alone, a
second install changes nothing, and removing takes back exactly what was added.
Everything runs in temporary folders; no real Claude settings are read.
"""

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from presto_ai_dash.collectors import claude_mod, presto_collector as collector

SETTING = claude_mod.PLUGIN_DIRS_SETTING
NOW = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)


class Terminal(io.StringIO):
    """Standard input that is, or is not, a person at a keyboard."""

    def __init__(self, answers="", interactive=True):
        io.StringIO.__init__(self, answers)
        self.interactive = interactive

    def isatty(self):
        return self.interactive


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "home" / ".presto-ai-dash"
        self.settings = self.root / "home" / ".claude" / "settings.json"
        self.place = claude_mod.locations(str(self.state), str(self.settings))

    def write_settings(self, data, raw=None):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        text = raw if raw is not None else json.dumps(data, indent=2) + "\n"
        self.settings.write_bytes(text.encode("utf-8") if isinstance(text, str) else text)
        return self.settings.read_bytes()

    def read_settings(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def install(self, **kwargs):
        return claude_mod.install(str(self.state), str(self.settings), now=NOW, **kwargs)

    def remove(self):
        return claude_mod.remove(str(self.state), str(self.settings), now=NOW)

    def backups(self):
        return claude_mod.list_backups(str(self.settings))

    def mod_dirs(self):
        return self.read_settings()["env"][SETTING].split(os.pathsep)


class InstallTests(Base):
    def test_a_missing_settings_file_is_created_with_only_the_setting(self):
        done = self.install()
        self.assertEqual(self.read_settings(), {"env": {SETTING: self.place["plugin_dir"]}})
        self.assertIsNone(done["backup"])
        self.assertTrue(done["settings_changed"] and done["files_changed"])

    def test_the_three_mod_files_are_written_and_the_output_folder_made(self):
        self.install()
        for relative in (".claude-plugin/plugin.json", "hooks/hooks.json", "hooks/register.js"):
            self.assertTrue((Path(self.place["plugin_dir"]) / relative).is_file(), relative)
        self.assertTrue(Path(self.place["out_dir"]).is_dir())
        register = (Path(self.place["plugin_dir"]) / "hooks" / "register.js").read_text(encoding="utf-8")
        self.assertIn(json.dumps(self.place["out_dir"] + os.sep), register)

    def test_the_output_folder_is_the_one_the_collector_reads_by_default(self):
        self.state.mkdir(parents=True)
        config = self.state / "collector.json"
        config.write_text(json.dumps({"url": "http://127.0.0.1:1/api/v1/ingest", "accounts": [
            {"service": "claude", "token_file": "t"}]}))
        account = collector.load_config(str(config))["accounts"][0]
        self.assertEqual(os.path.normcase(account["mod_folder"]),
                         os.path.normcase(self.place["out_dir"]))

    def test_everything_else_in_the_settings_is_kept_and_a_backup_is_made_first(self):
        original = self.write_settings({"model": "sonnet", "permissions": {"allow": ["Bash(ls)"]},
                                        "env": {"FOO": "bar"}})
        done = self.install()
        settings = self.read_settings()
        self.assertEqual(settings["model"], "sonnet")
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(settings["env"]["FOO"], "bar")
        self.assertEqual(Path(done["backup"]).read_bytes(), original)
        self.assertEqual(self.backups(), [done["backup"]])

    def test_an_existing_plugin_folder_setting_is_extended_not_replaced(self):
        other = str(self.root / "other-mod")
        self.write_settings({"env": {SETTING: other}})
        self.install()
        self.assertEqual(self.mod_dirs(), [other, self.place["plugin_dir"]])

    def test_a_second_install_changes_nothing(self):
        self.write_settings({"model": "sonnet"})
        self.install()
        before = self.settings.read_bytes()
        again = self.install()
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertFalse(again["settings_changed"] or again["files_changed"])
        self.assertIsNone(again["backup"])
        self.assertEqual(len(self.backups()), 1)

    def test_a_changed_mod_file_is_refreshed_without_another_backup(self):
        self.install()
        register = Path(self.place["plugin_dir"]) / "hooks" / "register.js"
        register.write_text("// an old version\n")
        again = self.install()
        self.assertTrue(again["files_changed"])
        self.assertFalse(again["settings_changed"])
        self.assertNotIn("an old version", register.read_text(encoding="utf-8"))

    def test_two_backups_in_one_second_do_not_overwrite_each_other(self):
        self.write_settings({"a": 1})
        first = claude_mod.make_backup(str(self.settings), NOW)
        second = claude_mod.make_backup(str(self.settings), NOW)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first) and os.path.exists(second))

    def test_a_byte_order_mark_from_a_windows_editor_is_accepted(self):
        self.write_settings(None, raw=b"\xef\xbb\xbf" + json.dumps({"model": "sonnet"}).encode())
        self.install()
        self.assertEqual(self.read_settings()["model"], "sonnet")


class RefusalTests(Base):
    def assert_untouched(self, original):
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(self.backups(), [])
        self.assertFalse(Path(self.place["plugin_dir"]).exists())
        self.assertFalse(Path(self.place["record"]).exists())

    def test_a_settings_file_that_is_not_plain_json_is_left_alone(self):
        for raw in ('{"model": "sonnet", // a comment\n}', "{not json", "", "[1, 2]", "null"):
            original = self.write_settings(None, raw=raw)
            with self.assertRaises(claude_mod.ModInstallError):
                self.install()
            self.assert_untouched(original)

    def test_an_env_setting_that_is_not_an_object_is_left_alone(self):
        original = self.write_settings({"env": ["FOO"]})
        with self.assertRaises(claude_mod.ModInstallError):
            self.install()
        self.assert_untouched(original)

    def test_a_plugin_folder_setting_that_is_not_text_is_left_alone(self):
        original = self.write_settings({"env": {SETTING: ["x"]}})
        with self.assertRaises(claude_mod.ModInstallError):
            self.install()
        self.assert_untouched(original)

    def test_a_settings_file_too_large_to_be_settings_is_left_alone(self):
        original = self.write_settings(None, raw=" " * (claude_mod.MAX_SETTINGS_BYTES + 1))
        with self.assertRaises(claude_mod.ModInstallError):
            self.install()
        self.assert_untouched(original)


class RemoveTests(Base):
    def test_removing_after_installing_restores_the_settings(self):
        original = {"model": "sonnet", "env": {"FOO": "bar"}}
        self.write_settings(original)
        self.install()
        done = self.remove()
        self.assertEqual(self.read_settings(), original)
        self.assertTrue(done["settings_changed"] and done["files_removed"])
        self.assertFalse(Path(self.place["plugin_dir"]).exists())

    def test_a_settings_file_the_install_created_is_deleted_again(self):
        self.install()
        self.remove()
        self.assertFalse(self.settings.exists())

    def test_an_env_block_the_install_created_goes_with_it(self):
        self.write_settings({"model": "sonnet"})
        self.install()
        self.remove()
        self.assertEqual(self.read_settings(), {"model": "sonnet"})

    def test_only_this_mods_folder_is_taken_out_of_the_list(self):
        other = str(self.root / "other-mod")
        self.write_settings({"env": {SETTING: other}})
        self.install()
        self.remove()
        self.assertEqual(self.read_settings(), {"env": {SETTING: other}})

    def test_removing_makes_a_backup_and_keeps_every_earlier_one(self):
        self.write_settings({"model": "sonnet"})
        self.install()
        self.remove()
        self.assertEqual(len(self.backups()), 2)

    def test_the_readings_the_mod_wrote_are_kept(self):
        self.install()
        kept = Path(self.place["out_dir"]) / "session-abc.json"
        kept.write_text("{}")
        self.remove()
        self.assertTrue(kept.exists())

    def test_the_mods_own_folder_is_tidied_away_but_nothing_else_in_it(self):
        self.install()
        base = Path(self.place["record"]).parent
        self.remove()
        self.assertFalse(base.exists())
        self.install()
        (base / "something-else.txt").write_text("keep me")
        self.remove()
        self.assertTrue((base / "something-else.txt").exists())

    def test_removing_when_nothing_is_installed_changes_nothing(self):
        original = self.write_settings({"model": "sonnet"})
        done = self.remove()
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertFalse(done["settings_changed"])
        self.assertEqual(self.backups(), [])

    def test_removing_works_without_the_install_record(self):
        self.write_settings({"model": "sonnet"})
        self.install()
        os.remove(self.place["record"])
        self.remove()
        self.assertNotIn("env", self.read_settings())

    def test_removing_does_not_touch_a_settings_file_it_cannot_read(self):
        original = self.write_settings(None, raw="{not json")
        with self.assertRaises(claude_mod.ModInstallError):
            self.remove()
        self.assertEqual(self.settings.read_bytes(), original)


class StatusTests(Base):
    def run_status(self):
        return claude_mod.status(str(self.state), str(self.settings), now=NOW.timestamp())

    def test_nothing_installed(self):
        lines, healthy = self.run_status()
        text = "\n".join(lines)
        self.assertIn("not installed", text)
        self.assertIn("does not list the mod", text)
        self.assertTrue(healthy)          # nothing was set up, so nothing is wrong

    def test_installed_and_current(self):
        self.install()
        lines, healthy = self.run_status()
        self.assertIn("present and current", "\n".join(lines))
        self.assertIn("lists the mod", "\n".join(lines))
        self.assertTrue(healthy)

    def test_files_without_the_setting_are_a_problem(self):
        self.install()
        self.remove_setting_only()
        lines, healthy = self.run_status()
        self.assertFalse(healthy)

    def remove_setting_only(self):
        self.settings.write_text("{}")

    def test_an_out_of_date_mod_is_a_problem_with_the_fix_named(self):
        self.install()
        (Path(self.place["plugin_dir"]) / "hooks" / "register.js").write_text("// old\n")
        lines, healthy = self.run_status()
        self.assertFalse(healthy)
        self.assertIn("claude-mod install", "\n".join(lines))

    def test_a_settings_file_that_cannot_be_read_is_reported_not_raised(self):
        self.write_settings(None, raw="{not json")
        lines, healthy = self.run_status()
        self.assertFalse(healthy)
        self.assertIn("not plain JSON", "\n".join(lines))

    def test_the_backup_count_is_shown(self):
        self.write_settings({"model": "sonnet"})
        self.install()
        self.assertIn("Settings backups: 1", "\n".join(self.run_status()[0]))


class DescribeTests(Base):
    def test_it_says_what_changes_where_how_to_undo_and_what_the_mod_reads(self):
        text = "\n".join(claude_mod.describe_install(str(self.state), str(self.settings), {}))
        for needle in (self.place["plugin_dir"], str(self.settings), "copy",
                       ".presto-backup-", "claude-mod remove", self.place["out_dir"],
                       "reads no", "no network request", "register.js"):
            self.assertIn(needle, text)

    def test_an_environment_that_already_sets_the_folders_is_pointed_out(self):
        text = "\n".join(claude_mod.describe_install(str(self.state), str(self.settings),
                                                     {SETTING: "/somewhere"}))
        self.assertIn("already sets", text)
        quiet = "\n".join(claude_mod.describe_install(str(self.state), str(self.settings), {}))
        self.assertNotIn("already sets", quiet)


class CommandTests(Base):
    def run_command(self, action, answers="", interactive=True, assume_yes=False):
        out = io.StringIO()
        code = collector.claude_mod_command(
            action, str(self.settings), str(self.state), assume_yes=assume_yes, out=out,
            stdin=Terminal(answers, interactive), now=NOW.timestamp())
        return code, out.getvalue()

    def test_install_explains_asks_and_then_does_it(self):
        self.write_settings({"model": "sonnet"})
        code, output = self.run_command("install", "y\n")
        self.assertEqual(code, 0, output)
        self.assertLess(output.index("What will change"), output.index("Install the mod? [y/N]"))
        self.assertIn("Backup of the previous file", output)
        self.assertIn("restart Claude Code".lower(), output.lower())
        self.assertIn(self.place["plugin_dir"], self.mod_dirs())

    def test_installing_again_says_so_without_asking_or_changing_anything(self):
        self.write_settings({"model": "sonnet"})
        self.run_command("install", assume_yes=True)
        before = self.settings.read_bytes()
        code, output = self.run_command("install", interactive=False)   # no question to answer
        self.assertEqual(code, 0, output)
        self.assertIn("already installed and up to date", output)
        self.assertNotIn("[y/N]", output)
        self.assertNotIn("What will change", output)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_an_installed_mod_with_old_files_is_refreshed_without_touching_the_settings(self):
        self.write_settings({"model": "sonnet"})
        self.run_command("install", assume_yes=True)
        before = self.settings.read_bytes()
        register = Path(self.place["plugin_dir"]) / "hooks" / "register.js"
        register.write_text("// an older collector's mod\n")
        code, output = self.run_command("install", interactive=False)
        self.assertEqual(code, 0, output)
        self.assertIn("refreshed", output)
        self.assertIn("Claude's settings were not changed", output)
        self.assertNotIn("older collector", register.read_text(encoding="utf-8"))
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_a_listed_mod_whose_files_were_deleted_is_put_back(self):
        self.run_command("install", assume_yes=True)
        import shutil
        shutil.rmtree(self.place["plugin_dir"])
        code, output = self.run_command("install", interactive=False)
        self.assertEqual(code, 0, output)
        self.assertTrue((Path(self.place["plugin_dir"]) / "hooks" / "register.js").is_file())

    def test_a_settings_file_that_cannot_be_read_is_still_explained_when_installing_again(self):
        self.run_command("install", assume_yes=True)
        self.write_settings(None, raw="{not json")
        code, output = self.run_command("install", interactive=False)
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", output)

    def test_an_answer_of_no_changes_nothing(self):
        original = self.write_settings({"model": "sonnet"})
        for answer in ("n\n", "\n", "maybe\n"):
            code, output = self.run_command("install", answer)
            self.assertEqual(code, 2)
            self.assertIn("Nothing was changed", output)
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(self.backups(), [])

    def test_with_no_terminal_it_refuses_unless_told_to_go_ahead(self):
        original = self.write_settings({"model": "sonnet"})
        code, output = self.run_command("install", interactive=False)
        self.assertEqual(code, 2)
        self.assertIn("--yes", output)
        self.assertEqual(self.settings.read_bytes(), original)
        code, _ = self.run_command("install", interactive=False, assume_yes=True)
        self.assertEqual(code, 0)

    def test_a_settings_file_it_cannot_change_is_explained_and_left_alone(self):
        original = self.write_settings(None, raw="{not json")
        code, output = self.run_command("install", "y\n")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was changed", output)
        self.assertIn("not plain JSON", output)
        self.assertEqual(self.settings.read_bytes(), original)

    def test_remove_asks_first_too(self):
        self.write_settings({"model": "sonnet"})
        self.run_command("install", assume_yes=True)
        code, _ = self.run_command("remove", "n\n")
        self.assertEqual(code, 2)
        self.assertIn(self.place["plugin_dir"], self.mod_dirs())
        code, output = self.run_command("remove", "y\n")
        self.assertEqual(code, 0, output)
        self.assertEqual(self.read_settings(), {"model": "sonnet"})

    def test_status_exit_code_says_whether_all_is_well(self):
        self.run_command("install", assume_yes=True)
        self.assertEqual(self.run_command("status")[0], 0)
        (Path(self.place["plugin_dir"]) / "hooks" / "register.js").write_text("// old\n")
        self.assertEqual(self.run_command("status")[0], 1)

    def test_the_command_line(self):
        out = io.StringIO()
        arguments = ["claude-mod", "install", "--yes", "--settings", str(self.settings),
                     "--state-dir", str(self.state)]
        from contextlib import redirect_stdout
        with redirect_stdout(out):
            code = collector.main(arguments)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn(self.place["plugin_dir"], self.mod_dirs())

    def test_the_settings_file_follows_claude_config_dir(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "profile")}):
            self.assertEqual(collector._claude_settings_default(),
                             str(self.root / "profile" / "settings.json"))

    def test_the_help_mentions_the_mod(self):
        self.assertIn("claude-mod install", collector.HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
