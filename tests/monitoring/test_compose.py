from pathlib import Path

import yaml

COMPOSE = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]


def test_the_bot_relaunches_only_on_failure_and_never_reads_stdin():
    bot = SERVICES["bot"]
    assert bot["restart"] == "on-failure"
    assert "stdin_open" not in bot
    assert bot.get("stop_signal", "SIGTERM") == "SIGTERM"
    assert "APPRISE_NOTIFICATION_URLS=${APPRISE_NOTIFICATION_URLS:-}" in bot["environment"]


def test_loki_publishes_no_port():
    assert "ports" not in SERVICES["loki"]


def test_monitoring_ports_are_bound_to_localhost():
    for name in ("grafana", "alloy"):
        for mapping in SERVICES[name].get("ports", []):
            assert str(mapping).startswith("127.0.0.1:"), (name, mapping)
