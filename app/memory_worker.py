"""Dedicated automatic-memory worker. No network extraction in an API response."""
import signal
import logging
from threading import Event

from app.composition.container import Services
from app.core.config import get_settings
from app.core.logging import configure_logging


def main():
    settings = get_settings()
    if not settings.openviking_enabled:
        raise SystemExit('Set OPENVIKING_ENABLED=true after the explicit database migration')
    configure_logging()
    stop = Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    services = Services(settings)
    try:
        while not stop.is_set():
            try:
                worked = services.automatic_memories.run_once()
            except Exception as error:
                logging.getLogger('rag').error('memory_worker_poll_failed', extra={'error_type': type(error).__name__})
                worked = False
            if not worked:
                stop.wait(settings.openviking_poll_seconds)
    finally:
        services.close()


if __name__ == '__main__':
    main()
