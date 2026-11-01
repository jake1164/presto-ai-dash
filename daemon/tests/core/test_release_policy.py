"""Which published release a running installation hears about, and which copy is running.

Pure logic: no network, no database. Registry adapters and the scheduler have
their own tests.
"""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import presto_ai_dash
from presto_ai_dash import release
from presto_ai_dash.release_policy import (Release, choose, eligible, parse_docker,
                                           parse_python, parse_running)


def releases(*labels, docker=False):
    parse = parse_docker if docker else parse_python
    return [Release(parse(label), label) for label in labels]


class TagTests(unittest.TestCase):
    def test_numbered_releases_are_parsed_in_their_own_spelling(self):
        self.assertEqual(parse_python("0.50.0rc4"), parse_docker("0.50.0-rc4"))
        self.assertEqual(parse_python("0.50.0"), parse_docker("0.50.0"))
        self.assertIsNone(parse_python("0.50.0-rc4"))
        self.assertIsNone(parse_docker("0.50.0rc4"))

    def test_running_labels_are_accepted_in_either_spelling(self):
        self.assertEqual(parse_running("0.50.0-rc4"), parse_running("0.50.0rc4"))

    def test_floating_aliases_and_look_alikes_are_not_releases(self):
        for tag in ("latest", "0", "0.50", "v0.50.0", "0.50.0.1", "0.50.0-rc", "0.50.0-rc4-amd64",
                    "0.50.0-dev1", "0.50.0.post1", "0.50.0+local", "1!0.50.0", "main",
                    "sha-3f9a", "0.50.0-RC4", " 0.50.0", "0.50.0\n", "", "99999.0.0",
                    "0.50.0-beta1", "0.50.0b"):
            with self.subTest(tag=tag):
                self.assertIsNone(parse_docker(tag))
                self.assertIsNone(parse_python(tag))

    def test_non_strings_are_not_releases(self):
        for value in (None, 4, ["0.50.0"], {"a": 1}):
            self.assertIsNone(parse_running(value))


class OrderingTests(unittest.TestCase):
    def test_a_candidate_orders_before_its_final_release_and_by_number(self):
        order = [parse_python(label) for label in
                 ("0.50.0a1", "0.50.0b1", "0.50.0rc2", "0.50.0rc10", "0.50.0", "0.50.1")]
        self.assertEqual(order, sorted(order))

    def test_numbers_compare_as_numbers_not_text(self):
        self.assertGreater(parse_python("0.50.10"), parse_python("0.50.9"))
        self.assertGreater(parse_docker("0.50.0-rc10"), parse_docker("0.50.0-rc9"))


class StablePolicyTests(unittest.TestCase):
    def test_a_stable_installation_hears_of_newer_stable_releases(self):
        outcome = choose("0.50.0", releases("0.50.0", "0.50.1", "0.51.0"))
        self.assertEqual((outcome.state, outcome.latest.label), ("available", "0.51.0"))

    def test_a_stable_installation_ignores_newer_candidates(self):
        outcome = choose("0.50.0", releases("0.50.0", "0.51.0rc1", "0.50.1rc2"))
        self.assertEqual(outcome.state, "current")
        self.assertEqual(outcome.latest.label, "0.50.0")

    def test_a_stable_installation_is_current_at_the_newest_stable_release(self):
        self.assertEqual(choose("0.51.0", releases("0.50.0", "0.51.0")).state, "current")

    def test_older_releases_are_never_offered(self):
        outcome = choose("0.51.0", releases("0.49.0", "0.50.0"))
        self.assertEqual(outcome.state, "current")
        self.assertIsNone(outcome.latest)


class CandidatePolicyTests(unittest.TestCase):
    def test_a_candidate_hears_of_a_later_candidate_of_its_release(self):
        outcome = choose("0.50.0rc4", releases("0.50.0rc3", "0.50.0rc4", "0.50.0rc5"))
        self.assertEqual((outcome.state, outcome.latest.label), ("available", "0.50.0rc5"))

    def test_the_container_spelling_orders_the_same_way(self):
        outcome = choose("0.50.0-rc4", releases("0.50.0-rc4", "0.50.0-rc5", docker=True))
        self.assertEqual((outcome.state, outcome.latest.label), ("available", "0.50.0-rc5"))

    def test_a_candidate_hears_of_the_final_release_over_its_candidates(self):
        outcome = choose("0.50.0rc4", releases("0.50.0rc5", "0.50.0", "0.50.0rc6"))
        self.assertEqual((outcome.state, outcome.latest.label), ("available", "0.50.0"))

    def test_a_candidate_ignores_candidates_of_other_releases(self):
        outcome = choose("0.50.0rc4", releases("0.50.0rc4", "0.51.0rc1"))
        self.assertEqual(outcome.state, "current")

    def test_a_candidate_hears_of_a_newer_stable_release_of_a_later_version(self):
        outcome = choose("0.50.0rc4", releases("0.51.0"))
        self.assertEqual((outcome.state, outcome.latest.label), ("available", "0.51.0"))

    def test_the_exact_spelling_of_the_published_release_is_kept(self):
        outcome = choose("0.50.0-rc4", releases("0.50.0-rc05", docker=True))
        self.assertEqual(outcome.latest.label, "0.50.0-rc05")

    def test_eligibility_is_strictly_newer(self):
        running = parse_python("0.50.0rc4")
        self.assertFalse(eligible(running, running))
        self.assertTrue(eligible(running, parse_python("0.50.0rc5")))

    def test_a_running_value_that_is_not_a_release_is_unknown_not_guessed(self):
        for running in ("latest", "dev", "0.50", "", "not a version"):
            self.assertIsNone(choose(running, releases("0.50.0")), running)


class AmbiguousWheelTests(unittest.TestCase):
    """Up to v0.50.0-rc3 a candidate wheel said 0.50.0 like the final release."""

    def test_without_a_published_final_release_the_wheel_is_an_early_candidate(self):
        outcome = choose("0.50.0", releases("0.50.0rc4", "0.50.0rc5"), ambiguous=True)
        self.assertEqual((outcome.state, outcome.latest.label), ("available", "0.50.0rc5"))
        self.assertEqual(outcome.running_note, "pre_release_unspecified")

    def test_it_is_offered_the_final_release_once_that_exists(self):
        # Published final 0.50.0 means the wheel is compared as that release.
        outcome = choose("0.50.0", releases("0.50.0rc5", "0.50.0", "0.50.1"), ambiguous=True)
        self.assertEqual((outcome.state, outcome.latest.label), ("available", "0.50.1"))
        self.assertEqual(outcome.running_note, "version_ambiguous")

    def test_it_keeps_a_note_even_when_current(self):
        outcome = choose("0.50.0", releases("0.50.0"), ambiguous=True)
        self.assertEqual(outcome.state, "current")
        self.assertEqual(outcome.running_note, "version_ambiguous")

    def test_no_candidate_number_is_invented(self):
        outcome = choose("0.50.0", releases("0.49.0"), ambiguous=True)
        self.assertEqual(outcome.state, "current")
        self.assertIsNone(outcome.latest)
        self.assertEqual(outcome.running_note, "pre_release_unspecified")

    def test_an_unambiguous_version_is_not_noted(self):
        self.assertIsNone(choose("0.50.1", releases("0.50.1")).running_note)


class FakeDistribution(object):
    """Enough of importlib.metadata.Distribution for installation detection."""

    def __init__(self, version="0.50.0rc4", own=True, files=None):
        self.version = version
        self._own = own
        self.files = {"INSTALLER": "pip\n", "RECORD": "x,,\n"}
        self.files.update(files or {})

    def locate_file(self, name):
        if self._own:
            return Path(presto_ai_dash.__file__)
        return Path("/elsewhere/site-packages") / name

    def read_text(self, name):
        return self.files.get(name)


class InstallationTests(unittest.TestCase):
    def kind(self, environ=None, container=False, distribution=None):
        return release.installation(environ or {}, container=container,
                                    distribution=distribution)

    def test_a_published_wheel_is_pypi_with_its_installed_version(self):
        found = self.kind(distribution=FakeDistribution("0.50.0rc4"))
        self.assertEqual((found.kind, found.channel, found.running),
                         ("pip", "pypi", "0.50.0rc4"))
        self.assertFalse(found.ambiguous)

    def test_a_wheel_needs_no_build_time_override_to_be_published(self):
        # The absence of DASH_RELEASE_VERSION must not make a wheel development.
        self.assertEqual(self.kind(distribution=FakeDistribution("0.51.0")).channel, "pypi")

    def test_the_final_wheel_is_not_mistaken_for_an_early_candidate(self):
        # rc1-rc3 also said 0.50.0, but none of them contains this check, so
        # code running it as 0.50.0 is the final release.
        found = self.kind(distribution=FakeDistribution("0.50.0"))
        self.assertEqual((found.channel, found.running, found.ambiguous),
                         ("pypi", "0.50.0", False))

    def test_the_running_code_must_be_the_installed_files(self):
        self.assertEqual(self.kind(distribution=FakeDistribution(own=False)).channel,
                         "development")

    def test_a_source_tree_egg_info_is_not_an_installed_wheel(self):
        found = self.kind(distribution=FakeDistribution(files={"INSTALLER": None, "RECORD": None}))
        self.assertEqual(found.channel, "development")

    def test_editable_directory_and_vcs_installs_are_development(self):
        for info in ({"dir_info": {"editable": True}, "url": "file:///x"},
                     {"dir_info": {}, "url": "file:///x"},
                     {"vcs_info": {"vcs": "git"}, "url": "git+https://x"}):
            with self.subTest(info=info):
                found = self.kind(distribution=FakeDistribution(
                    files={"direct_url.json": json.dumps(info)}))
                self.assertEqual(found.channel, "development")

    def test_a_downloaded_release_wheel_is_still_a_published_build(self):
        info = {"archive_info": {"hash": "sha256=ab"}, "url": "file:///dl/p.whl"}
        found = self.kind(distribution=FakeDistribution(
            files={"direct_url.json": json.dumps(info)}))
        self.assertEqual(found.channel, "pypi")

    def test_a_missing_or_unreadable_distribution_is_development(self):
        class Broken(FakeDistribution):
            def locate_file(self, name):
                raise OSError("gone")
        self.assertEqual(self.kind(distribution=Broken()).channel, "development")

    def test_a_published_image_is_ghcr_with_its_exact_embedded_release(self):
        found = self.kind({"DASH_RELEASE_VERSION": "0.50.0-rc4"}, container=True)
        self.assertEqual((found.kind, found.channel, found.running),
                         ("docker", "ghcr", "0.50.0-rc4"))
        self.assertFalse(found.ambiguous)

    def test_a_wheel_inside_an_image_is_not_asked_about_pypi(self):
        found = self.kind({"DASH_RELEASE_VERSION": "0.50.0-rc4"}, container=True,
                          distribution=FakeDistribution("0.50.0rc4"))
        self.assertEqual(found.channel, "ghcr")

    def test_a_locally_built_image_is_development(self):
        found = self.kind({}, container=True, distribution=FakeDistribution("0.50.0rc4"))
        self.assertEqual((found.channel, found.reason), ("development", "local_image"))

    def test_an_unusable_image_label_is_development(self):
        found = self.kind({"DASH_RELEASE_VERSION": "has space"}, container=True)
        self.assertEqual(found.channel, "development")

    def test_a_checkout_is_development(self):
        found = self.kind({})
        self.assertEqual((found.channel, found.reason), ("development", "checkout"))

    def test_this_test_run_is_a_checkout(self):
        self.assertEqual(release.installation({}).channel, "development")


if __name__ == "__main__":
    unittest.main()
