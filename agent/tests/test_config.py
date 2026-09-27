import pytest

from porch_light.config import ConfigError, load_config


def test_example_config_loads(config, tmp_path):
    assert config.presence.outer_radius_m == 400 and config.presence.inner_radius_m == 200
    assert config.llm.timeout_s == 30 and config.llm.base_url == "https://openrouter.ai/api/v1"
    assert config.policy_file == tmp_path / "policy.md"
    assert config.event_log == tmp_path / "logs" / "arrivals.jsonl"
    assert config.llm.api_key is None
    assert config.presence.max_fix_age_s == 120


def test_secrets_from_env_file(tmp_path, monkeypatch):
    for name in ("OPENROUTER_API_KEY", "OWM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text("home: {lat: 1, lon: 2, timezone: UTC}\n")
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-or-test\nOWM_API_KEY=owm\n")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.llm.api_key == "sk-or-test" and cfg.weather.api_key == "owm"


@pytest.mark.parametrize("yaml_text,message", [
    ("presence: {}\n", "home.lat"),
    ("home: {lat: 99, lon: 0}\n", "out of range"),
    ("home: {lat: 1, lon: 2, timezone: Mars/Base}\n", "timezone"),
    ("home: {lat: 1, lon: 2}\npresence: {outer_radius_m: 100, inner_radius_m: 200}\n", "inner_radius_m"),
    ("home: {lat: 1, lon: 2}\nbulb: {kelvin_min: 7000, kelvin_max: 2000}\n", "kelvin_min"),
    ("home: {lat: 1, lon: 2}\nbulb: {min_brightness_pct: 0}\n", "brightness"),
    ("home: {lat: one, lon: 2}\n", "number"),
    ("home: {lat: 1, lon: 2}\npresence: {max_fix_age_s: -5}\n", "max_fix_age_s"),
])
def test_invalid_config(tmp_path, yaml_text, message):
    (tmp_path / "config.yaml").write_text(yaml_text)
    with pytest.raises(ConfigError, match=message):
        load_config(tmp_path / "config.yaml", env_file=tmp_path / "none")


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config(tmp_path / "nope.yaml")


def test_duplicate_key_is_rejected_with_line_numbers(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "home: {lat: 1, lon: 2}\n"
        "mqtt:\n"
        "  username: porch\n"
        "  port: 1883\n"
        "  username:\n"
    )
    with pytest.raises(ConfigError, match=r"line 5: 'username' is defined twice \(first on line 3\)"):
        load_config(tmp_path / "config.yaml", env_file=tmp_path / "none")


def test_duplicate_section_is_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "home: {lat: 1, lon: 2}\nmqtt:\n  host: a\nmqtt:\n  username: porch\n"
    )
    with pytest.raises(ConfigError, match="'mqtt' is defined twice"):
        load_config(tmp_path / "config.yaml", env_file=tmp_path / "none")


def test_same_key_in_different_sections_is_fine(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "home: {lat: 1, lon: 2}\n"
        "mqtt: {timeout: 1}\n"
        "llm: {timeout_s: 20}\n"
        "mcp: {timeout_s: 5}\n"
    )
    cfg = load_config(tmp_path / "config.yaml", env_file=tmp_path / "none")
    assert (cfg.llm.timeout_s, cfg.mcp.timeout_s) == (20, 5)


def test_invalid_yaml_is_a_config_error(tmp_path):
    (tmp_path / "config.yaml").write_text("home: {lat: 1, lon: 2\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(tmp_path / "config.yaml", env_file=tmp_path / "none")
