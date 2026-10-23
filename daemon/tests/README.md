# Test groups

Run commands from `daemon/` with the repository's `.venv`:

```powershell
# Everything (also used by CI)
../.venv/Scripts/python -m unittest discover -s tests -q

# One group; replace providers with any folder below
../.venv/Scripts/python -m unittest discover -s tests/providers -t tests -q

# One file, optionally filtered to a class or method with -k
../.venv/Scripts/python -m unittest discover -s tests/providers -t tests -p test_sources.py -k OllamaTests -v
```

On macOS/Linux, use `../.venv/bin/python`.

| Folder | Coverage |
| --- | --- |
| `core/` | Models, storage, quotas, effective configuration, alert thresholds |
| `admin/` | Account management, admin routes, sessions, settings |
| `providers/` | Provider clients, Claude readers, Codex, source templates |
| `ingest/` | Ingest protocol and storage, pushed readings, remote collector |
| `device/` | Presto app, drawing, navigation, network, device API contract |
| `security/` | Input validation, authentication, HTTP limits, private files |
| `tooling/` | Preview, USB deployment, Docker, docs, health checks, licences |

Each group needs its `__init__.py` for unittest discovery. Keep `-t tests`
when running a group so imports use the same root as the full suite. Shared
helpers (`envutil.py`, `presto_path.py`) and fixture data stay at this root.
Use group-qualified imports, such as
`from admin.test_management import ManagementBase`, for shared test fixtures.

VS Code's Testing view reflects these folders after **Test: Refresh Tests**.
The **Run test group** task provides a group picker; **Run all tests** runs
the full suite. More details: [Running the tests](../../docs/user/developers/running-the-tests.md).
