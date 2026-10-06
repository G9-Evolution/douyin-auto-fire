from pathlib import Path
import sys

import pytest

import run as run_module


@pytest.fixture(autouse=True)
def clean_cli_args(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py"])


def _write_accounts(tmp_path: Path, content: str) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "accounts.json").write_text(content, encoding="utf-8")


def test_no_accounts_file_uses_single_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run_module, "run_single", lambda: 42)
    monkeypatch.setattr(run_module, "run_all_accounts", lambda only=None: 7)

    assert run_module.main() == 42


def test_accounts_file_uses_multi_mode(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _write_accounts(tmp_path, '{"accounts": [{"id": "a", "env_file": ".env.a"}]}')
    monkeypatch.setattr(run_module, "run_single", lambda: 42)
    monkeypatch.setattr(run_module, "run_all_accounts", lambda only=None: 7)

    assert run_module.main() == 7


def test_broken_accounts_file_returns_2(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _write_accounts(tmp_path, "not json")
    monkeypatch.setattr(run_module, "run_single", lambda: 42)
    monkeypatch.setattr(run_module, "run_all_accounts", lambda only=None: 7)

    assert run_module.main() == 2


def test_multi_mode_interrupt_returns_130(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    _write_accounts(tmp_path, '{"accounts": []}')
    monkeypatch.setattr(run_module, "run_single", lambda: 42)

    def interrupted(only=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module, "run_all_accounts", interrupted)

    assert run_module.main() == 130


@pytest.mark.parametrize("prevent_duplicates", [True, False, None])
def test_workbench_friend_command_reaches_account_runner(monkeypatch, tmp_path, prevent_duplicates):
    """Use the command produced by the workbench; stop at a simulated sender."""
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import Mock

    import app.account_runner as runner
    import web_server

    monkeypatch.chdir(tmp_path)
    account = SimpleNamespace(id="fixture_account", env_file=tmp_path / ".env.fixture")
    calls = []
    validation_calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(run_module, "load_accounts", lambda: [account])
    monkeypatch.setattr(runner, "load_accounts", lambda: [account])
    monkeypatch.setattr(runner, "account_env", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(runner, "load_settings", lambda _: SimpleNamespace(
        task_config_path=tmp_path / "fixture-task.json", artifacts_dir=tmp_path / "artifacts"))
    monkeypatch.setattr(runner, "_configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "LOGGER", Mock())
    monkeypatch.setattr(runner, "require_validation", lambda *args: validation_calls.append(args))
    monkeypatch.setattr(runner, "run", fake_run)
    for key in runner._LEGACY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(web_server, "_run_tasks", {})
    monkeypatch.setattr(web_server, "_run_seq", 0)

    class FakeProcess:
        pid = 123

        def __init__(self, command, **kwargs):
            monkeypatch.setattr(sys, "argv", command[1:])
            self.returncode = run_module.main()
            assert self.returncode == 0

    class FakeThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(web_server.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(web_server.threading, "Thread", FakeThread)
    web_server._spawn_run(False, account.id, "fixture_friend", "fixture-revision", prevent_duplicates)

    expected = {"dry_run": False, "target_name": "fixture_friend", "task_revision": "fixture-revision"}
    if prevent_duplicates is not None:
        expected["prevent_duplicates"] = prevent_duplicates
    assert calls == [expected]
    assert len(validation_calls) == 1
    assert validation_calls[0][0] == account.id


@pytest.mark.parametrize("extra_args", [
    ["--allow-duplicates"],
    ["--target", "fixture_friend", "--account", "fixture_account", "--source", "scheduled", "--allow-duplicates"],
])
def test_duplicate_override_requires_manual_selected_friend(monkeypatch, extra_args):
    monkeypatch.setattr(sys, "argv", ["run.py", *extra_args])
    monkeypatch.setattr(run_module, "load_accounts", lambda: pytest.fail("invalid scope must stop before loading accounts"))
    assert run_module.main() == 2


def test_multi_account_override_without_account_stops_before_dispatch(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", "--target", "fixture_friend", "--prevent-duplicates"])
    monkeypatch.setattr(run_module, "load_accounts", lambda: [object()])
    monkeypatch.setattr(run_module, "run_all_accounts", lambda **kwargs: pytest.fail("account is required"))
    assert run_module.main() == 2


@pytest.mark.parametrize("flag, expected", [("--prevent-duplicates", True), ("--allow-duplicates", False)])
def test_legacy_single_account_accepts_duplicate_override(monkeypatch, flag, expected):
    monkeypatch.setattr(sys, "argv", ["run.py", "--target", "fixture_friend", flag])
    monkeypatch.setattr(run_module, "load_accounts", lambda: None)
    calls = []

    def fake_single():
        from app.main import _parse_cli_args
        args = _parse_cli_args()
        calls.append((args.target, args.prevent_duplicates))
        return 0

    monkeypatch.setattr(run_module, "run_single", fake_single)
    assert run_module.main() == 0
    assert calls == [("fixture_friend", expected)]


def test_conflicting_duplicate_flags_stop_before_dispatch(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run.py", "--prevent-duplicates", "--allow-duplicates"])
    monkeypatch.setattr(run_module, "load_accounts", lambda: pytest.fail("conflicting flags must stop"))
    with pytest.raises(SystemExit) as exc:
        run_module.main()
    assert exc.value.code == 2
