"""``python -m backend.hosthelper --config /etc/jendock/host-helper.json [--check]``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from backend.hosthelper.apache import ApacheControl
from backend.hosthelper.config import HelperConfig, load_config
from backend.hosthelper.errors import HelperError
from backend.hosthelper.server import HelperServer
from backend.hosthelper.service import HelperService

logger = logging.getLogger("backend.hosthelper")


def build_service(config: HelperConfig) -> HelperService:
    """Create the service with the configured apachectl."""
    return HelperService(config, ApacheControl(config.apachectl, config.command_timeout_seconds))


def serve(config: HelperConfig) -> None:
    """Serve until SIGTERM or SIGINT."""
    server = HelperServer(config, build_service(config))
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25})
    thread.start()
    logger.info("host helper listening on %s", config.socket_path)
    stop.wait()
    server.shutdown()
    thread.join()
    server.server_close()


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(prog="jendock-host-helper")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="validate the configuration and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        if args.check:
            result = build_service(config).execute("apache.configtest", {})
            print(f"configuration ok; apache configtest ok={result['ok']}")
            return 0 if result["ok"] else 1
        serve(config)
    except (HelperError, OSError) as error:
        print(f"host helper: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
