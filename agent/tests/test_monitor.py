"""Location monitor: detector, dispatcher, MQTT wiring (mocked) and simulation files."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from porch_light.monitor import (
    ArrivalDetector,
    ArrivalDispatcher,
    load_fixes,
    run_mqtt,
    run_simulation,
)

SIM = Path(__file__).resolve().parent.parent / "sim"
M_PER_DEG_LAT = 111_195.0


def fix(config, north_m, acc=15, **extra):
    return json.dumps({"_type": "location", "lat": config.home.lat + north_m / M_PER_DEG_LAT,
                       "lon": config.home.lon, "acc": acc, **extra})


def test_detector_fires_once(config):
    detector = ArrivalDetector(config)
    fires = []
    for d in (1000, 500, 350, 150, 300, 20):
        obs = detector.process(fix(config, d))
        fires.append(obs.transition is not None and obs.transition.fire)
    assert fires == [False, False, True, False, False, False]


def test_detector_drops_inaccurate_fixes_before_state_machine(config):
    detector = ArrivalDetector(config)
    detector.process(fix(config, 1000))
    assert detector.process(fix(config, 100, acc=250)) is None
    assert detector.tracker.state.value == "away"


def test_detector_ignores_other_messages(config):
    detector = ArrivalDetector(config)
    assert detector.process(json.dumps({"_type": "lwt"})) is None


def test_dispatcher_single_flight():
    started = []

    async def handler(distance):
        started.append(distance)
        await asyncio.sleep(0.05)

    async def main():
        dispatcher = ArrivalDispatcher(handler)
        first = dispatcher.dispatch(390)
        second = dispatcher.dispatch(380)
        await dispatcher.wait()
        third = dispatcher.dispatch(370)
        await dispatcher.wait()
        return first, second, third

    first, second, third = asyncio.run(main())
    assert first is not None and second is None and third is not None
    assert started == [390, 370]


class FakeMqttClient:
    """Enough of paho's Client to drive run_mqtt from a test."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.subscribed = []
        self.connected_to = None

    def username_pw_set(self, *a):
        pass

    def reconnect_delay_set(self, **kw):
        pass

    def connect_async(self, host, port, keepalive):
        self.connected_to = (host, port)

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def loop_start(self):
        # paho would connect, then deliver messages from its network thread.
        self.on_connect(self, None, None, SimpleNamespace(is_failure=False), None)
        for p in self.payloads:
            self.on_message(self, None, SimpleNamespace(payload=p.encode()))

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


def test_run_mqtt_fires_handler_once(config):
    payloads = [fix(config, d) for d in (1200, 600, 390, 250, 150, 350, 50)]
    client = FakeMqttClient(payloads)
    arrivals = []
    done = asyncio.Event

    async def main():
        finished = done()

        async def handler(distance):
            arrivals.append(round(distance))
            finished.set()

        task = asyncio.create_task(run_mqtt(config, handler, client_factory=lambda: client))
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0.05)  # let the remaining fixes drain
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert arrivals == [390]
    assert client.subscribed == [config.mqtt.topic]
    assert client.connected_to == ("localhost", 1883)


def test_load_fixes_skips_comments_and_adds_type(tmp_path):
    f = tmp_path / "fixes.jsonl"
    f.write_text('# comment\n\n{"lat": 1, "lon": 2, "acc": 5}\n{"_type": "lwt"}\n')
    docs = [json.loads(p) for p in load_fixes(f)]
    assert docs == [{"lat": 1, "lon": 2, "acc": 5, "_type": "location"}, {"_type": "lwt"}]


def test_load_fixes_reports_bad_line(tmp_path):
    f = tmp_path / "bad.jsonl"
    f.write_text('{"lat": 1}\nnope\n')
    with pytest.raises(ValueError, match=":2:"):
        load_fixes(f)


@pytest.mark.parametrize("name,expected", [("arrival.jsonl", 1), ("leave-and-return.jsonl", 2)])
def test_shipped_simulations(config, name, expected, capsys):
    arrivals = []

    async def handler(distance):
        arrivals.append(distance)

    asyncio.run(run_simulation(config, handler, load_fixes(SIM / name)))
    assert len(arrivals) == expected
    assert all(200 <= d < 400 for d in arrivals)
    assert "ARRIVAL" in capsys.readouterr().out


# --- stale fixes -----------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from porch_light.presence import Presence  # noqa: E402

NOW = datetime(2026, 11, 20, 18, 30, tzinfo=timezone.utc)


def aged(config, north_m, age_s, acc=15):
    return fix(config, north_m, acc=acc, tst=int((NOW - timedelta(seconds=age_s)).timestamp()))


def detector_at(config, **kw):
    return ArrivalDetector(config, clock=lambda: NOW, **kw)


def test_fresh_fix_fires(config):
    d = detector_at(config)
    d.process(aged(config, 900, 30))
    obs = d.process(aged(config, 350, 10))
    assert obs.transition.fire and not obs.stale and obs.age_s == 10


def test_old_fix_does_not_fire(config, caplog):
    d = detector_at(config)
    d.process(aged(config, 900, 1000))
    with caplog.at_level("INFO"):
        obs = d.process(aged(config, 350, 600))
    assert obs.stale and not obs.transition.fire and obs.transition.suppressed
    assert d.tracker.state is Presence.APPROACHING
    assert "NOT fired" in caplog.text and "600 s old" in caplog.text


def test_age_limit_boundary(config):
    for age, fires in ((120, True), (121, False)):
        d = detector_at(config)
        d.process(aged(config, 900, age))
        assert d.process(aged(config, 350, age)).transition.fire is fires


def test_queued_backlog_after_reconnect_does_not_fire_late(config):
    """Phone offline on the walk home; the queue arrives once you are indoors."""
    d = detector_at(config)
    d.process(aged(config, 1500, 900))            # last fix before going offline
    fired = []
    for north, age in ((800, 600), (350, 420), (150, 360), (10, 300)):   # queued
        obs = d.process(aged(config, north, age))
        fired.append(obs.transition is not None and obs.transition.fire)
    obs = d.process(aged(config, 5, 2))           # first live fix, at the door
    fired.append(obs.transition is not None and obs.transition.fire)
    assert not any(fired)
    assert d.tracker.state is Presence.NEAR


def test_backlog_then_live_approach_still_fires(config):
    """Queued fixes from far away, then a live fix inside the radius: a real arrival."""
    d = detector_at(config)
    for north, age in ((2000, 900), (1200, 600)):
        d.process(aged(config, north, age))
    assert d.process(aged(config, 380, 5)).transition.fire


def test_fix_without_timestamp_counts_as_current(config):
    d = detector_at(config)
    d.process(fix(config, 900))
    obs = d.process(fix(config, 350))
    assert obs.age_s is None and obs.transition.fire


def test_future_timestamp_counts_as_current(config):
    d = detector_at(config)
    d.process(aged(config, 900, -30))
    assert d.process(aged(config, 350, -30)).transition.fire


def test_zero_disables_the_check(config):
    d = detector_at(config, max_fix_age_s=0)
    d.process(aged(config, 900, 99999))
    assert d.process(aged(config, 350, 99999)).transition.fire


def test_simulation_ignores_fix_age(config):
    # The shipped simulations carry 2026-09 timestamps; they must still fire.
    arrivals = []

    async def handler(distance):
        arrivals.append(distance)

    payloads = [aged(config, n, 10**7) for n in (900, 350, 100)]
    asyncio.run(run_simulation(config, handler, payloads))
    assert len(arrivals) == 1


def test_mqtt_backlog_does_not_fire(config, monkeypatch):
    """Through run_mqtt: a stale backlog followed by a live fix at the door."""
    import porch_light.monitor as monitor

    class FrozenDetector(ArrivalDetector):
        def __init__(self, cfg):
            super().__init__(cfg, clock=lambda: NOW)

    monkeypatch.setattr(monitor, "ArrivalDetector", FrozenDetector)
    payloads = [aged(config, n, a) for n, a in ((1500, 900), (350, 400), (100, 380), (5, 1))]
    client = FakeMqttClient(payloads)
    arrivals = []

    async def main():
        async def handler(distance):
            arrivals.append(distance)

        task = asyncio.create_task(run_mqtt(config, handler, client_factory=lambda: client))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert arrivals == []
