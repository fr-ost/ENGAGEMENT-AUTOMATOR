"""Environment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

DEFAULT_SMM_API_URL = "https://bulkfollows.com/api/v2"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(repr=False)
    owner_ids: frozenset
    view_service_id: str
    like_service_id: str
    smm_api_url: str = DEFAULT_SMM_API_URL
    data_dir: Path = Path("data")
    default_views: int = 1000
    default_likes: int = 30
    spread_hours: float = 12.0
    max_views: int = 100_000
    max_likes: int = 5_000
    max_users: int = 10


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env
    errors: list[str] = []

    def get(name: str) -> str:
        return (env.get(name) or "").strip()

    def required(name: str) -> str:
        value = get(name)
        if not value:
            errors.append(f"{name} is required")
        return value

    def number(name: str, default, cast=int):
        raw = get(name)
        if not raw:
            return default
        try:
            value = cast(raw)
        except ValueError:
            errors.append(f"{name} must be a number")
            return default
        if value < 0:
            errors.append(f"{name} must not be negative")
        return value

    token = required("TELEGRAM_BOT_TOKEN")
    view_service = required("SMM_VIEW_SERVICE_ID")
    like_service = required("SMM_LIKE_SERVICE_ID")

    owner_ids = set()
    for part in required("TELEGRAM_OWNER_ID").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            owner_ids.add(int(part))
        elif part:
            errors.append("TELEGRAM_OWNER_ID must be numeric Telegram user IDs (the admins), comma-separated")

    url = get("SMM_API_URL") or DEFAULT_SMM_API_URL
    if not url.startswith("https://"):
        errors.append("SMM_API_URL must be an https:// URL")

    cfg = dict(
        default_views=number("DEFAULT_VIEWS", 1000),
        default_likes=number("DEFAULT_LIKES", 30),
        spread_hours=number("SPREAD_HOURS", 12.0, float),
        max_views=number("MAX_VIEWS", 100_000),
        max_likes=number("MAX_LIKES", 5_000),
        max_users=number("MAX_USERS", 10),
    )
    if errors:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))
    return Config(
        telegram_token=token,
        owner_ids=frozenset(owner_ids),
        view_service_id=view_service,
        like_service_id=like_service,
        smm_api_url=url.rstrip("/"),
        data_dir=Path(get("DATA_DIR") or "data"),
        **cfg,
    )
