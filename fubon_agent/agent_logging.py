import logging
import sys
from datetime import datetime, timedelta

from fubon_agent import get_config

config = get_config()

LOG_FILE_PATH = config.get("logging", {}).get("log_file_path") if config.get("logging") else None
LOG_FORMATTER = logging.Formatter("[%(levelname)s] %(message)s")
LOG_LEVEL = config.get("logging", {}).get("log_level", "INFO") if config.get("logging") else "INFO"


def get_console_handler():
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(LOG_FORMATTER)
    return console_handler


def get_file_handler(log_file):
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(LOG_FORMATTER)
    return file_handler


def get_logger(logger_name):
    tw_now_time = datetime.utcnow() + timedelta(hours=8)
    logger = logging.getLogger(logger_name)
    logger.setLevel(LOG_LEVEL)
    logger.addHandler(get_console_handler())
    if LOG_FILE_PATH:
        logger.addHandler(
            get_file_handler(
                LOG_FILE_PATH.replace("{date}", tw_now_time.strftime("%Y%m%d"))
            )
        )
    # with this pattern, it's rarely necessary to propagate the error up to parent
    logger.propagate = False
    return logger
