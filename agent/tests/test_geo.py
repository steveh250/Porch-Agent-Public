import math

import pytest

from porch_light.geo import haversine_m

M_PER_DEG_LAT = 111_195.0


def test_zero_distance():
    assert haversine_m(51.5, -0.14, 51.5, -0.14) == 0


def test_one_degree_of_latitude():
    assert haversine_m(0, 0, 1, 0) == pytest.approx(M_PER_DEG_LAT, rel=1e-3)


def test_short_north_south_offset():
    # 400 m north of a point in London.
    lat = 51.501 + 400 / M_PER_DEG_LAT
    assert haversine_m(lat, -0.142, 51.501, -0.142) == pytest.approx(400, abs=1)


def test_east_west_shrinks_with_latitude():
    at_equator = haversine_m(0, 0, 0, 0.01)
    at_60 = haversine_m(60, 0, 60, 0.01)
    assert at_60 == pytest.approx(at_equator * math.cos(math.radians(60)), rel=1e-3)


def test_symmetric():
    assert haversine_m(51.5, -0.1, 48.85, 2.35) == pytest.approx(haversine_m(48.85, 2.35, 51.5, -0.1))


def test_known_city_pair():
    # London (Charing Cross) to Paris (Notre-Dame): ~343.5 km great circle.
    assert haversine_m(51.5074, -0.1278, 48.8530, 2.3499) / 1000 == pytest.approx(343.5, abs=2)


def test_antipodes_do_not_overflow():
    assert haversine_m(0, 0, 0, 180) == pytest.approx(math.pi * 6_371_008.8)
