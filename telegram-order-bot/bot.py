"""Entry point: python bot.py"""

from __future__ import annotations

import logging
import sys

from orderbot.app import App
from orderbot.config import ConfigError, load_config
from orderbot.panel import Panel
from orderbot.store import Store
from orderbot.tg import Telegram


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # its debug lines include the token URL
    try:
        config = load_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    store = Store(config.data_dir / "orderbot.sqlite3")
    App(config, store, Panel(config.smm_api_url, config.smm_api_key), Telegram(config.telegram_token)).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
