"""Start the web app and experiment worker together."""

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def available_port(host, first):
    if not 1 <= first <= 65535:
        raise ValueError("Порт должен быть от 1 до 65535")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in range(first, min(first + 100, 65536)):
        with socket.socket(family, socket.SOCK_STREAM) as listener:
            try:
                listener.bind((host, port))
            except OSError as exc:
                import errno

                if exc.errno != errno.EADDRINUSE:
                    raise
            else:
                return port
    raise RuntimeError("Не найден свободный порт")


def stop_processes(processes):
    for process in reversed(processes):
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 15
    for process in reversed(processes):
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait()


def serve(host, port, environment):
    processes = []
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        stopping = True

    handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}

    def start(arguments):
        process = subprocess.Popen(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=environment,
            start_new_session=True,
        )
        processes.append(process)
        return process

    try:
        bootstrap = start(["scripts/bootstrap.py"])
        while bootstrap.poll() is None and not stopping:
            time.sleep(0.1)
        if stopping:
            return 0
        if bootstrap.returncode:
            return bootstrap.returncode

        web = start(
            [
                "-m",
                "uvicorn",
                "finbot.main:app",
                "--host",
                host,
                "--port",
                str(port),
                "--log-level",
                "warning",
            ]
        )
        worker = start(["-m", "finbot.experiments.worker"])
        display_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in display_host:
            display_host = f"[{display_host}]"
        print(f"\nЭксперименты: http://{display_host}:{port}/vendor/experiments", flush=True)
        print("Запускаются веб-приложение и исполнитель. Остановить оба: Ctrl+C.\n", flush=True)
        while not stopping:
            for label, process in (("Веб-приложение", web), ("Исполнитель", worker)):
                code = process.poll()
                if code is not None:
                    print(f"{label} остановлено (код {code}).", file=sys.stderr, flush=True)
                    return code if code > 0 else 1
            time.sleep(0.2)
        return 0
    finally:
        stop_processes(processes)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def main():
    from finbot.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=settings.PORT)
    args = parser.parse_args()
    try:
        port = available_port(args.host, args.port)
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Не удалось выбрать порт: {exc}\n")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(args.host, args.host)
    if ":" in host:
        host = f"[{host}]"
    environment = {
        **os.environ,
        "HOST": args.host,
        "PORT": str(port),
        "MAGIC_LINK_BASE_URL": f"http://{host}:{port}",
        "PYTHONUNBUFFERED": "1",
    }
    environment.setdefault("LOG_LEVEL", "WARNING")
    if port != args.port:
        print(f"Порт {args.port} занят, используется {port}.", flush=True)
    return serve(args.host, port, environment)


if __name__ == "__main__":
    raise SystemExit(main())
