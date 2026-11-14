import logging

import pytest

from src.config import ConfigError, RedactingFilter, load_config, load_dotenv


def base(**extra):
    env = {"X_BEARER_TOKEN": "tok-abcdef", "X_USERNAME": "@someone"}
    env.update(extra)
    return env


def test_defaults():
    cfg = load_config(base())
    assert cfg.x_username == "someone"
    assert cfg.impression_target == 1000 and cfg.like_target == 30
    assert cfg.include_quote_posts is True
    assert cfg.smm_enabled is False and cfg.telegram_enabled is False


def test_smm_defaults_to_bulkfollows_and_derives_provider():
    cfg = load_config(base(SMM_ENABLED="true", SMM_API_KEY="k"))
    assert cfg.smm_api_url == "https://bulkfollows.com/api/v2"
    assert cfg.smm_provider == "bulkfollows"
    other = load_config(base(SMM_API_URL="https://smmsoc.com/api/v2"))
    assert other.smm_provider == "smmsoc"


def test_missing_required_values_listed_together():
    with pytest.raises(ConfigError) as excinfo:
        load_config({})
    assert "X_BEARER_TOKEN" in str(excinfo.value) and "X_USERNAME" in str(excinfo.value)


@pytest.mark.parametrize(
    "extra,needle",
    [
        ({"SMM_ENABLED": "true"}, "SMM_API_KEY"),
        ({"SMM_ENABLED": "true", "SMM_API_URL": "http://insecure.test", "SMM_API_KEY": "k"}, "https"),
        ({"TELEGRAM_ENABLED": "true"}, "TELEGRAM_BOT_TOKEN"),
        ({"X_MAX_PAGES": "0"}, "X_MAX_PAGES"),
        ({"X_USER_ID": "abc"}, "digits"),
        ({"SCAN_SINCE": "last tuesday"}, "SCAN_SINCE"),
        ({"DRY_RUN": "maybe"}, "DRY_RUN"),
    ],
)
def test_invalid_values(extra, needle):
    with pytest.raises(ConfigError, match=needle):
        load_config(base(**extra))


def test_repr_hides_secrets():
    cfg = load_config(base(SMM_API_KEY="smm-secret", TELEGRAM_BOT_TOKEN="tg-secret"))
    text = repr(cfg)
    assert "tok-abcdef" not in text and "smm-secret" not in text and "tg-secret" not in text


def test_redacting_filter():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token is %s", ("tok-abcdef",), None)
    RedactingFilter(["tok-abcdef"]).filter(record)
    assert "tok-abcdef" not in record.getMessage()


def test_load_dotenv_does_not_override_and_strips_comments(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "X_USERNAME=fromfile   # inline comment\n"
        "export X_MAX_PAGES=3\n"
        'QUOTED="a # not a comment"\n'
        "X_BEARER_TOKEN=file-token\n"
    )
    env = {"X_BEARER_TOKEN": "real-env-token"}
    load_dotenv(env_file, env)
    assert env["X_USERNAME"] == "fromfile"
    assert env["X_MAX_PAGES"] == "3"
    assert env["QUOTED"] == "a # not a comment"
    assert env["X_BEARER_TOKEN"] == "real-env-token"
    assert load_dotenv(tmp_path / "missing.env", env) == 0
