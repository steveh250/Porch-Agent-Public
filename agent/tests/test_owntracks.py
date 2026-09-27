import json

import pytest

from porch_light.owntracks import is_accurate, parse_location


def payload(**fields):
    doc = {"_type": "location", "lat": 51.5, "lon": -0.14, "acc": 20, "tst": 1790000000}
    doc.update(fields)
    return json.dumps(doc).encode()


def test_parses_location():
    fix = parse_location(payload())
    assert (fix.lat, fix.lon, fix.acc) == (51.5, -0.14, 20)
    assert fix.tst.year == 2026


@pytest.mark.parametrize("kind", ["transition", "waypoint", "lwt", "card", None])
def test_ignores_other_message_types(kind):
    assert parse_location(payload(_type=kind)) is None


@pytest.mark.parametrize("bad", [b"not json", b"[]", b"", "\xff".encode("latin-1")])
def test_ignores_garbage(bad):
    assert parse_location(bad) is None


@pytest.mark.parametrize("field,value", [("lat", None), ("lat", "51.5"), ("lat", 91),
                                         ("lon", 181), ("lat", True)])
def test_rejects_bad_coordinates(field, value):
    assert parse_location(payload(**{field: value})) is None


def test_rejects_missing_accuracy():
    doc = json.loads(payload())
    del doc["acc"]
    assert parse_location(json.dumps(doc)) is None


def test_missing_timestamp_is_fine():
    doc = json.loads(payload())
    del doc["tst"]
    assert parse_location(json.dumps(doc)).tst is None


@pytest.mark.parametrize("acc,ok", [(0, True), (50, True), (100, True), (100.1, False), (250, False)])
def test_accuracy_filter(acc, ok):
    assert is_accurate(parse_location(payload(acc=acc)), 100) is ok
