import logging

_LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S"

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

logger = get_logger(__name__)