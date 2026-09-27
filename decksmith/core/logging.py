import logging


def setup_logging(level: int = logging.INFO) -> None:
    """Настройка логирования; шумные библиотеки — только предупреждения."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpcore", "PIL", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
