import logging
from config import Config


logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(Config.APP_NAME)