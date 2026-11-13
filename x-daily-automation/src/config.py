"""Configuration loading, validation, and secret-safe logging."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlparse

# Bangladesh Standard Time is a fixed UTC+6 offset with no daylight saving,
# so a fixed offset avoids depending on the system tz database.
BD_TZ = timezone(timedelta(hours=6), "Asia/Dhaka")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SMM_API_URL = "https://bulkfollows.com/api/v2"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    # X API
    x_bearer_token: str
    x_username: Optional[str]
    x_user_id: Optional[str]
    include_quote_posts: bool = True
    x_page_size: int = 100
    x_max_pages: int = 5
    initial_lookback_hours: int = 24
    scan_overlap_hours: int = 2
    scan_since_override: Optional[datetime] = None
    metrics_lookback_days: int = 7

    # HTTP behaviour shared by all clients
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 3
    max_rate_limit_wait_seconds: int = 120

    # Promotion planning
    impression_target: int = 1000
    like_target: int = 30
    promotion_budget: Optional[float] = None
    promotion_budget_currency: str = "USD"

    # SMM panel (BulkFollows by default)
    smm_enabled: bool = False
    smm_auto_order: bool = False  # place orders automatically for new posts
    smm_api_url: str = DEFAULT_SMM_API_URL
    smm_provider: str = "bulkfollows"  # derived from the API URL; stored on each order
    smm_api_key: Optional[str] = field(default=None, repr=False)
    smm_impression_service_id: Optional[str] = None
    smm_like_service_id: Optional[str] = None

    # Telegram
    telegram_enabled: bool = False
    telegram_bot_token: Optional[str] = field(default=None, repr=False)
    telegram_chat_id: Optional[str] = None

    # Cost estimation (only used when the user supplies verified prices)
    x_cost_per_request_usd: Optional[float] = None
    x_cost_per_post_read_usd: Optional[float] = None

    # Runtime
    data_dir: Path = PROJECT_ROOT / "data"
    reports_dir: Path = PROJECT_ROOT / "reports"
    dry_run: bool = False
    allow_state_reinit: bool = False
    log_level: str = "INFO"

    def __repr__(self) -> str:  # never print secrets, even by accident
        return f"Config(x_username={self.x_username!r}, x_user_id={self.x_user_id!r}, dry_run={self.dry_run})"

    def secret_values(self) -> list[str]:
        values = [self.x_bearer_token, self.smm_api_key, self.telegram_bot_token]
        return [v for v in values if v and len(v) >= 4]


def _get(env: Mapping[str, str], name: str) -> Optional[str]:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return default if raw == "" else False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _float(env: Mapping[str, str], name: str, minimum: float = 0.0) -> Optional[float]:
    raw = _get(env, name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def parse_iso_datetime(raw: str) -> datetime:
    """Parse an ISO-8601 timestamp; naive values are treated as UTC."""
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_iso(value: datetime) -> str:
    """Format a datetime as UTC ISO-8601 with a trailing Z (X API style)."""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _https_url(name: str, raw: str) -> str:
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigError(f"{name} must be an https:// URL")
    return raw.rstrip("/")


def _provider_name(url: str) -> str:
    """bulkfollows.com -> "bulkfollows"; keeps orders from different panels apart."""
    labels = (urlparse(url).hostname or "").split(".")
    return labels[-2] if len(labels) >= 2 else labels[0] or "smm"


def load_config(env: Optional[Mapping[str, str]] = None) -> Config:
    env = os.environ if env is None else env
    errors: list[str] = []

    bearer = _get(env, "X_BEARER_TOKEN")
    if not bearer:
        errors.append("X_BEARER_TOKEN is required")
    username = _get(env, "X_USERNAME")
    if username:
        username = username.lstrip("@")
    user_id = _get(env, "X_USER_ID")
    if user_id and not user_id.isdigit():
        errors.append("X_USER_ID must contain digits only")
    if not username and not user_id:
        errors.append("Set X_USERNAME (recommended) or X_USER_ID")

    scan_since = None
    raw_since = _get(env, "SCAN_SINCE")
    if raw_since:
        try:
            scan_since = parse_iso_datetime(raw_since)
        except ValueError:
            errors.append("SCAN_SINCE must be an ISO-8601 timestamp, e.g. 2026-01-31T00:00:00Z")

    smm_enabled = _bool(env, "SMM_ENABLED", False)
    smm_auto_order = _bool(env, "SMM_AUTO_ORDER", False)
    smm_url = _get(env, "SMM_API_URL") or DEFAULT_SMM_API_URL
    smm_key = _get(env, "SMM_API_KEY")
    if smm_enabled and not smm_key:
        errors.append("SMM_ENABLED=true requires SMM_API_KEY")
    try:
        smm_url = _https_url("SMM_API_URL", smm_url)
    except ConfigError as exc:
        errors.append(str(exc))

    telegram_enabled = _bool(env, "TELEGRAM_ENABLED", False)
    telegram_token = _get(env, "TELEGRAM_BOT_TOKEN")
    telegram_chat = _get(env, "TELEGRAM_CHAT_ID")
    if telegram_enabled and not (telegram_token and telegram_chat):
        errors.append("TELEGRAM_ENABLED=true requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    try:
        kwargs = dict(
            include_quote_posts=_bool(env, "INCLUDE_QUOTE_POSTS", True),
            x_page_size=_int(env, "X_PAGE_SIZE", 100, 5, 100),
            x_max_pages=_int(env, "X_MAX_PAGES", 5, 1, 50),
            initial_lookback_hours=_int(env, "INITIAL_LOOKBACK_HOURS", 24, 1, 24 * 30),
            scan_overlap_hours=_int(env, "SCAN_OVERLAP_HOURS", 2, 0, 72),
            metrics_lookback_days=_int(env, "METRICS_LOOKBACK_DAYS", 7, 0, 90),
            http_timeout_seconds=float(_int(env, "HTTP_TIMEOUT_SECONDS", 20, 1, 120)),
            http_max_retries=_int(env, "HTTP_MAX_RETRIES", 3, 0, 10),
            max_rate_limit_wait_seconds=_int(env, "MAX_RATE_LIMIT_WAIT_SECONDS", 120, 0, 900),
            impression_target=_int(env, "IMPRESSION_TARGET", 1000, 0, 10**9),
            like_target=_int(env, "LIKE_TARGET", 30, 0, 10**9),
            promotion_budget=_float(env, "PROMOTION_BUDGET"),
            promotion_budget_currency=_get(env, "PROMOTION_BUDGET_CURRENCY") or "USD",
            x_cost_per_request_usd=_float(env, "X_COST_PER_REQUEST_USD"),
            x_cost_per_post_read_usd=_float(env, "X_COST_PER_POST_READ_USD"),
            dry_run=_bool(env, "DRY_RUN", False),
            allow_state_reinit=_bool(env, "ALLOW_STATE_REINIT", False),
        )
    except ConfigError as exc:
        errors.append(str(exc))
        kwargs = {}

    data_dir = Path(_get(env, "DATA_DIR") or PROJECT_ROOT / "data")
    reports_dir = Path(_get(env, "REPORTS_DIR") or PROJECT_ROOT / "reports")
    log_level = (_get(env, "LOG_LEVEL") or "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        errors.append("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")

    if errors:
        raise ConfigError("Invalid configuration:\n  - " + "\n  - ".join(errors))

    return Config(
        x_bearer_token=bearer,  # type: ignore[arg-type]
        x_username=username,
        x_user_id=user_id,
        scan_since_override=scan_since,
        smm_enabled=smm_enabled,
        smm_auto_order=smm_auto_order,
        smm_api_url=smm_url,
        smm_provider=_provider_name(smm_url),
        smm_api_key=smm_key,
        smm_impression_service_id=_get(env, "SMM_IMPRESSION_SERVICE_ID"),
        smm_like_service_id=_get(env, "SMM_LIKE_SERVICE_ID"),
        telegram_enabled=telegram_enabled,
        telegram_bot_token=telegram_token,
        telegram_chat_id=telegram_chat,
        data_dir=data_dir,
        reports_dir=reports_dir,
        log_level=log_level,
        **kwargs,
    )


def load_dotenv(path: Path, env: Optional[dict] = None) -> int:
    """Load KEY=VALUE lines from a local .env file without overriding real env vars.

    Supports comments, blank lines, optional "export ", quoted values, and
    inline " # comments" after unquoted values. Returns the number of keys set.
    """
    env = os.environ if env is None else env
    if not path.is_file():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key and key not in env:
            env[key] = value
            count += 1
    return count


def redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")
    return text


class RedactingFilter(logging.Filter):
    """Removes known secret values from every log record."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            record.msg = redact(message, self._secrets)
            record.args = None
        return True


def setup_logging(config: Optional[Config] = None) -> None:
    level = getattr(logging, config.log_level if config else "INFO")
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    if config:
        handler.addFilter(RedactingFilter(config.secret_values()))
    root.addHandler(handler)
    # urllib3 debug logs can include full request lines; keep them quiet.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
