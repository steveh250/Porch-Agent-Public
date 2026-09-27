import pytest

from porch_light.presence import Presence, PresenceTracker


def run(distances, outer=400, inner=200):
    tracker = PresenceTracker(outer, inner)
    fires = []
    for d in distances:
        t = tracker.update(d)
        fires.append(bool(t and t.fire))
    return tracker, fires


def test_first_fix_away_does_not_fire():
    tracker, fires = run([1000])
    assert tracker.state is Presence.AWAY and fires == [False]


def test_first_fix_at_home_does_not_fire():
    # Restarting the monitor while at home is not an arrival.
    tracker, fires = run([10, 50, 300])
    assert tracker.state is Presence.NEAR and not any(fires)


def test_arrival_fires_once_on_crossing_outer():
    tracker, fires = run([1000, 600, 399, 250, 199, 50])
    assert fires == [False, False, True, False, False, False]
    assert tracker.state is Presence.NEAR


def test_approaching_then_near():
    tracker = PresenceTracker(400, 200)
    tracker.update(800)
    t1 = tracker.update(300)
    t2 = tracker.update(150)
    assert (t1.previous, t1.current, t1.fire) == (Presence.AWAY, Presence.APPROACHING, True)
    assert (t2.previous, t2.current, t2.fire) == (Presence.APPROACHING, Presence.NEAR, False)


def test_jump_straight_from_away_to_near_fires():
    # Sparse fixes can skip the band between the radii entirely.
    _, fires = run([900, 100])
    assert fires == [False, True]


def test_no_retrigger_while_wobbling_inside_outer():
    _, fires = run([900, 350, 150, 350, 399, 150, 400, 300, 50])
    assert fires.count(True) == 1


def test_exactly_on_outer_radius_is_not_away_or_arrival():
    tracker, fires = run([900, 400])
    assert tracker.state is Presence.AWAY and fires == [False, False]
    tracker, fires = run([900, 100, 400])
    assert tracker.state is Presence.NEAR and fires.count(True) == 1


def test_rearms_only_beyond_outer():
    _, fires = run([900, 300, 100, 401, 300])
    assert fires == [False, True, False, False, True]


def test_leave_and_return_twice():
    _, fires = run([10, 700, 2000, 380, 100, 500, 300, 20])
    assert fires == [False, False, False, True, False, False, True, False]


def test_approaching_back_out_to_away_rearms_without_firing():
    tracker, fires = run([900, 300, 450])
    assert tracker.state is Presence.AWAY and fires == [False, True, False]


def test_repeated_same_state_returns_none():
    tracker = PresenceTracker(400, 200)
    tracker.update(900)
    assert tracker.update(950) is None


@pytest.mark.parametrize("outer,inner", [(200, 400), (400, 400), (400, 0)])
def test_invalid_radii(outer, inner):
    with pytest.raises(ValueError):
        PresenceTracker(outer, inner)


def test_stale_fix_moves_state_but_does_not_fire():
    tracker = PresenceTracker(400, 200)
    tracker.update(900)
    t = tracker.update(300, may_fire=False)
    assert (t.current, t.fire, t.suppressed) == (Presence.APPROACHING, False, True)


def test_suppressed_arrival_is_not_retried_by_a_later_fresh_fix():
    # The arrival belonged to the stale fix; once NEAR, a fresh fix changes nothing.
    tracker = PresenceTracker(400, 200)
    tracker.update(900)
    tracker.update(300, may_fire=False)
    tracker.update(150, may_fire=False)
    assert tracker.update(20) is None


def test_stale_non_arrival_transitions_are_not_marked_suppressed():
    tracker = PresenceTracker(400, 200)
    tracker.update(100)
    t = tracker.update(900, may_fire=False)
    assert (t.current, t.fire, t.suppressed) == (Presence.AWAY, False, False)


def test_rearm_after_stale_backlog_then_fresh_arrival_fires():
    tracker = PresenceTracker(400, 200)
    for d in (20, 800, 1500):  # left home; these arrive late
        tracker.update(d, may_fire=False)
    assert tracker.update(350).fire
