"""The Claude usage-cache reader does not follow links.

A link in place of the cache would make the daemon read whatever it points at.
The reader looks at the path without following it, and opens the file it looked
at and no other.
"""

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from presto_ai_dash.collectors import claude_usage_cache
from test_claude_usage_cache import CacheBase, cache_document


def make_link(link, target):
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError) as error:
        raise unittest.SkipTest("symbolic links are not available here: %s" % error)


class SymlinkTests(CacheBase):
    def test_a_link_to_a_real_cache_is_not_read(self):
        real = Path(self.temp.name) / "real.json"
        real.write_text('{"unrelated": true}', encoding="utf-8")
        make_link(self.path, real)
        reader = self.reader()
        with patch("builtins.open", side_effect=AssertionError("must not open")):
            result = self.refresh(reader)
        self.assertIs(result["available"], False)
        self.assertEqual(result["reason"], claude_usage_cache.REASON_NOT_FILE)
        self.assertIn("symbolic link", result["detail"].lower() if "detail" in result else str(result).lower())

    def test_a_simulated_link_is_not_read(self):
        self.write()
        real_lstat = os.lstat
        import stat

        def lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if str(path) == str(self.path):
                return os.stat_result((stat.S_IFLNK | 0o777,) + tuple(info)[1:])
            return info

        with patch("os.lstat", side_effect=lstat), \
                patch("builtins.open", side_effect=AssertionError("must not open")):
            result = self.refresh()
        self.assertEqual(result["reason"], claude_usage_cache.REASON_NOT_FILE)

    def test_a_file_swapped_after_the_check_is_not_read(self):
        self.write()
        other = Path(self.temp.name) / "other.json"
        other.write_text('{"secret": "do not read"}', encoding="utf-8")
        real_fstat = os.fstat
        swapped = os.stat(other)

        def fstat(descriptor):
            info = real_fstat(descriptor)
            return swapped if stat_is_regular(info) else info

        with patch("os.fstat", side_effect=fstat):
            result = self.refresh()
        self.assertIs(result["available"], False)
        self.assertNotIn("do not read", str(result))

    def test_an_ordinary_file_is_still_read(self):
        self.write(cache_document())
        self.assertIs(self.refresh()["available"], True)


def stat_is_regular(info):
    import stat
    return stat.S_ISREG(info.st_mode)


class SourceTests(unittest.TestCase):
    def test_the_reader_checks_without_following(self):
        source = Path(claude_usage_cache.__file__).read_text(encoding="utf-8")
        self.assertIn("os.lstat(", source)
        self.assertNotIn("os.stat(", source)
        self.assertIn("O_NOFOLLOW", source)


if __name__ == "__main__":
    unittest.main()
