import errno
import signal
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
