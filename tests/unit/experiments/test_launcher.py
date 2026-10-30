import errno
import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts import start_local as launcher


def test_busy_port_uses_next_without_ignoring_other_errors():
    listener = MagicMock()
    listener.__enter__.return_value = listener
    listener.bind.side_effect = [OSError(errno.EADDRINUSE, "busy"), None]
    with patch.object(launcher.socket, "socket", return_value=listener):
        assert launcher.available_port("127.0.0.1", 8000) == 8001
        listener.bind.side_effect = PermissionError(errno.EACCES, "denied")
        with pytest.raises(PermissionError):
            launcher.available_port("127.0.0.1", 8000)
    with pytest.raises(ValueError):
        launcher.available_port("127.0.0.1", 0)


def process(code=None):
    child = MagicMock()
    child.returncode = code
    child.poll.return_value = code
    return child


def test_bootstrap_failure_does_not_start_services():
    bootstrap = process(2)
    with (
        patch.object(launcher.subprocess, "Popen", return_value=bootstrap) as start,
        patch.object(launcher, "stop_processes") as stop,
    ):
        assert launcher.serve("127.0.0.1", 8001, {}) == 2
    assert start.call_count == 1
    stop.assert_called_once_with([bootstrap])


def test_worker_failure_stops_web():
    children = [process(0), process(), process(3)]
    with (
        patch.object(launcher.subprocess, "Popen", side_effect=children),
        patch.object(launcher, "stop_processes") as stop,
    ):
        assert launcher.serve("127.0.0.1", 8001, {}) == 3
    stop.assert_called_once_with(children)


def test_ctrl_c_stops_both_and_returns_success():
    children = [process(0), process(), process()]

    def interrupt(_):
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

    with (
        patch.object(launcher.subprocess, "Popen", side_effect=children),
        patch.object(launcher.time, "sleep", side_effect=interrupt),
        patch.object(launcher, "stop_processes") as stop,
    ):
        assert launcher.serve("127.0.0.1", 8001, {}) == 0
    stop.assert_called_once_with(children)


def test_cleanup_terminates_owned_process_groups():
    children = [process(0), process(), process()]
    for index, child in enumerate(children):
        child.pid = 1000 + index
    with patch.object(launcher.os, "killpg") as kill:
        launcher.stop_processes(children)
    assert kill.call_args_list == [
        ((1002, signal.SIGTERM),),
        ((1001, signal.SIGTERM),),
    ]
    assert all(child.wait.called for child in children)


@pytest.mark.parametrize("failed_child", [0, 1])
def test_container_failure_stops_both_services(failed_child):
    children = [process(), process()]
    children[failed_child] = process(3)
    with (
        patch.object(launcher.subprocess, "Popen", side_effect=children) as start,
        patch.object(launcher, "stop_processes") as stop,
    ):
        assert launcher.serve("0.0.0.0", 8000, {"WEB_CONCURRENCY": "2"}, container=True) == 3
    commands = [call.args[0] for call in start.call_args_list]
    assert len(commands) == 2
    assert commands[0][1:3] == ["-m", "gunicorn"]
    assert commands[0][commands[0].index("--workers") + 1] == "2"
    assert commands[1][1:] == ["-m", "finbot.experiments.worker"]
    stop.assert_called_once_with(children, timeout=65)


def test_container_sigterm_stops_both_services():
    children = [process(), process()]

    def terminate(_):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    with (
        patch.object(launcher.subprocess, "Popen", side_effect=children),
        patch.object(launcher.time, "sleep", side_effect=terminate),
        patch.object(launcher, "stop_processes") as stop,
    ):
        assert launcher.serve("0.0.0.0", 8000, {}, container=True) == 0
    stop.assert_called_once_with(children, timeout=65)


def test_container_uses_fixed_internal_port_and_preserves_public_url(monkeypatch):
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("MAGIC_LINK_BASE_URL", "https://finbot.example")
    with (
        patch.object(launcher.sys, "argv", ["start_local.py", "--container"]),
        patch.object(launcher, "available_port") as choose_port,
        patch.object(launcher, "serve", return_value=0) as serve,
    ):
        assert launcher.main() == 0
    choose_port.assert_not_called()
    host, port, environment = serve.call_args.args
    assert (host, port) == ("0.0.0.0", 8000)
    assert environment["PORT"] == "8000"
    assert environment["MAGIC_LINK_BASE_URL"] == "https://finbot.example"
    assert serve.call_args.kwargs == {"container": True}


def test_cleanup_kills_process_group_after_grace_period():
    child = process()
    child.pid = 1234
    child.wait.side_effect = [subprocess.TimeoutExpired("worker", 0), 0]
    with patch.object(launcher.os, "killpg") as kill:
        launcher.stop_processes([child], timeout=0)
    assert kill.call_args_list == [
        ((1234, signal.SIGTERM),),
        ((1234, signal.SIGKILL),),
    ]
