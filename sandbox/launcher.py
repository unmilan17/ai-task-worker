"""Starts all three sandbox apps (SupplierHub, ERP, Webmail) in one process.

    python -m sandbox.launcher            # reset state and serve on 8001-8003
    python -m sandbox.launcher --no-reset # keep the current ERP state

Each app gets its own uvicorn server running in a background thread.
"""

from __future__ import annotations

import argparse
import socket
import threading
import time

import uvicorn

from . import config, state


def loopback_sockets(port: int) -> list[socket.socket]:
    """Bind both 127.0.0.1 and ::1. On Windows, 'localhost' resolves to ::1 first; a server
    listening only on IPv4 makes every new client connection wait ~2 s before falling back."""
    socks = []
    s4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s4.bind((config.HOST, port))
    socks.append(s4)
    try:
        s6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s6.bind(("::1", port))
        socks.append(s6)
    except OSError:
        pass   # no IPv6 loopback on this machine - IPv4 only is fine
    return socks


def serve_in_thread(app, port: int, name: str) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    socks = loopback_sockets(port)
    threading.Thread(target=server.run, kwargs={"sockets": socks}, name=name, daemon=True).start()
    return server


def _serve(app, port: int) -> uvicorn.Server:
    return serve_in_thread(app, port, f"sandbox-{port}")


def start(reset: bool = True) -> list[uvicorn.Server]:
    """Start the sandbox in background threads (used by the CLI, tests and evals)."""
    from . import erp, mail, supplierhub

    if reset:
        state.reset()
    else:
        state.current_seed()
    servers = [
        _serve(supplierhub.app, config.SUPPLIERHUB_PORT),
        _serve(erp.app, config.ERP_PORT),
        _serve(mail.app, config.MAIL_PORT),
    ]
    deadline = time.time() + 15
    while not all(s.started for s in servers):
        if time.time() > deadline:
            raise RuntimeError("Sandbox servers did not start (are ports 8001-8003 free?)")
        time.sleep(0.05)
    return servers


def stop(servers: list[uvicorn.Server]) -> None:
    for s in servers:
        s.should_exit = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the simulated company environment")
    parser.add_argument("--no-reset", action="store_true", help="keep existing ERP state")
    parser.add_argument("--chaos", choices=["off", "mild", "hard"], default=None)
    args = parser.parse_args()
    if args.chaos:
        from .chaos import chaos
        chaos.set_mode(args.chaos)
    start(reset=not args.no_reset)
    print("Sandbox running:")
    print(f"  SupplierHub  {config.SUPPLIERHUB_URL}")
    print(f"  ERP          {config.ERP_URL}   (API docs: {config.ERP_URL}/api/docs)")
    print(f"  Webmail      {config.MAIL_URL}")
    print("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
