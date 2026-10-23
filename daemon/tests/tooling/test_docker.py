"""The container files: what may go into the image, and what may not.

The image must never carry a configuration file, a database, an encryption
key or the device's credentials. The compose file must publish the device
port on the LAN and the admin page on this machine only, and it must not
mount the user's Claude files into the container.
"""

from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

DOCKERIGNORE = ROOT / ".dockerignore"
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "compose.yaml"


def lines(path):
    """The meaningful lines of a text file."""
    text = path.read_text(encoding="utf-8")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


class DockerIgnoreTests(unittest.TestCase):
    """Every private name must be excluded from the build context."""

    REQUIRED = ("dash.toml", "dash.db", "dash.db.key", "presto/config.py",
                "presto/secrets.py", ".venv", ".git", ".preview", "daemon/tests")

    def setUp(self):
        self.entries = lines(DOCKERIGNORE)

    def test_every_private_name_is_excluded(self):
        for name in self.REQUIRED:
            with self.subTest(name=name):
                self.assertIn(name, self.entries)

    def test_the_test_data_is_excluded(self):
        self.assertIn("daemon/tests", self.entries)

    def test_the_containers_own_tokens_are_excluded(self):
        self.assertIn(".env", self.entries)

    def test_copies_and_variants_of_private_files_are_excluded(self):
        for name in (".env.*", "dash.toml.*", "*.bak", ".venv-*"):
            with self.subTest(name=name):
                self.assertIn(name, self.entries)
        gitignore = lines(ROOT / ".gitignore")
        for name in (".env.*", "!.env.example", "dash.toml.*", "*.bak", ".venv-*/"):
            with self.subTest(gitignore=name):
                self.assertIn(name, gitignore)


class DockerfileTests(unittest.TestCase):
    """The image: small, pinned, and not running as root."""

    def test_the_python_packages_are_pinned_and_installed_from_the_pins(self):
        pins = lines(ROOT / "docker" / "requirements.txt")
        self.assertTrue(pins)
        for pin in pins:
            with self.subTest(pin=pin):
                self.assertRegex(pin, r"^[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*$")
        names = [pin.split("==")[0].lower() for pin in pins]
        self.assertIn("cryptography", names)
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("-r requirements.txt", text)
        self.assertIn("--no-deps ./daemon", text)

    def test_the_pinned_cryptography_meets_the_daemons_lower_bound(self):
        project = (ROOT / "daemon" / "pyproject.toml").read_text(encoding="utf-8")
        bound = int(re.search(r"cryptography>=(\d+)", project).group(1))
        pinned = next(pin for pin in lines(ROOT / "docker" / "requirements.txt")
                      if pin.lower().startswith("cryptography=="))
        self.assertGreaterEqual(int(pinned.split("==")[1].split(".")[0]), bound)

    def test_the_final_image_has_no_pip(self):
        final = self.text.split("FROM ")[-1]
        self.assertRegex(final, r"rm -rf /usr/local/lib/python[0-9.]+/site-packages/pip")

    def setUp(self):
        self.text = DOCKERFILE.read_text(encoding="utf-8")
        self.entries = lines(DOCKERFILE)

    def test_the_base_image_is_pinned(self):
        bases = [entry for entry in self.entries if entry.startswith("FROM ")]
        self.assertTrue(bases)
        for base in bases:
            self.assertIn("python:", base)
            self.assertIn("@sha256:", base)
            self.assertNotIn(":latest", base)

    def test_the_image_runs_as_a_non_root_user(self):
        users = [entry for entry in self.entries if entry.startswith("USER ")]
        self.assertTrue(users)
        for user in users:
            self.assertNotIn("root", user)

    def test_the_data_directory_is_a_volume(self):
        self.assertIn('VOLUME ["/data"]', self.entries)
        self.assertIn("DASH_DATABASE=/data/dash.db", self.text)

    def test_nothing_private_is_copied_into_the_image(self):
        copied = [entry for entry in self.entries if entry.startswith("COPY ")]
        for entry in copied:
            for name in ("dash.toml", "dash.db", "presto/", ".env"):
                self.assertNotIn(name, entry)

    def test_the_health_check_asks_the_health_route(self):
        self.assertIn("/healthz", self.text)


class ComposeTests(unittest.TestCase):
    """The compose file: the ports, the volume and the time zone."""

    def setUp(self):
        self.text = COMPOSE.read_text(encoding="utf-8")
        self.entries = lines(COMPOSE)

    def test_the_device_port_is_published_so_the_presto_can_reach_it(self):
        self.assertIn(":8787:8787", self.text)

    def test_the_admin_port_is_published_on_this_machine_only(self):
        self.assertIn('"127.0.0.1:8788:8788"', self.text)
        # No admin port without the loopback prefix.
        for entry in self.entries:
            if re.match(r'^"?\d+:\d+"?$', entry):
                self.assertTrue(entry.startswith('"8787:'), entry)

    def test_the_data_lives_in_a_named_volume(self):
        self.assertIn("- dash-data:/data", self.text)
        self.assertIn("dash-data:", self.text)

    def test_the_device_port_address_can_be_chosen(self):
        self.assertIn('"${DASH_PUBLISH_ADDRESS:-0.0.0.0}:8787:8787"', self.text)

    def test_the_shared_token_is_required_so_none_is_logged(self):
        self.assertRegex(self.text, r'DASH_SHARED_TOKEN: "\$\{DASH_SHARED_TOKEN:\?')

    def test_the_container_is_hardened(self):
        for entry in ("read_only: true", "- no-new-privileges:true", "- ALL", "- /tmp"):
            with self.subTest(entry=entry):
                self.assertIn(entry, self.entries)
        self.assertIn("cap_drop:", self.entries)
        self.assertIn("tmpfs:", self.entries)

    def test_the_container_restarts(self):
        self.assertIn("restart: unless-stopped", self.text)

    def test_the_time_zone_is_set(self):
        self.assertIn("TZ:", self.text)

    def test_the_environment_variables_are_the_documented_ones(self):
        for name in ("DASH_HOST", "DASH_PORT", "DASH_DATABASE", "DASH_KEY_FILE",
                     "DASH_LOG_LEVEL", "DASH_SHARED_TOKEN", "DASH_ADMIN_TOKEN",
                     "DASH_ADMIN_HOST", "DASH_ADMIN_ALLOWED_HOSTS",
                     "DASH_INGEST_ENABLED", "DASH_ENCRYPTION_KEY",
                     "DASH_OVERFLOW_ROWS"):
            with self.subTest(name=name):
                self.assertIn(name + ":", self.text)

    def test_no_token_is_written_in_the_file(self):
        # Only placeholders: the values come from the environment.
        for entry in self.entries:
            for name in ("DASH_ADMIN_TOKEN", "DASH_SHARED_TOKEN",
                         "DASH_ENCRYPTION_KEY"):
                if entry.startswith(name + ":"):
                    value = entry.split(":", 1)[1].strip()
                    self.assertIn("${", value)

    def test_the_users_claude_files_are_never_mounted(self):
        for entry in self.entries:
            self.assertNotIn(".claude", entry)
            self.assertNotIn("claude.json", entry)
        self.assertNotIn(".claude", self.text.replace("~/.claude", ""))


if __name__ == "__main__":
    unittest.main()
