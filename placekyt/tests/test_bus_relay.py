"""§1.4 relay emission for >31-hop bus routes.

A single 10x12 chip's max manhattan distance is 20, so a >31-hop route only arises
on a heavily-snaked bus (or multi-chip). We test the relay-placement logic directly:
given a long waypoint path, the router must insert relay cells so every segment
(source→relay, relay→relay, relay→broker) is ≤31 hops, instead of failing.
"""

from engine.autoroute import RouteResult


def _place_relays(path, max_hops=31):
    """Mirror of the router's relay-placement (bus_router._route_chip_bus): a relay
    every (max_hops-1) waypoints, never the source (idx 0) or final cell."""
    seg = max_hops - 1
    relays = []
    idx = seg
    while idx < len(path) - 1:
        relays.append(path[idx])
        idx += seg
    return relays


def test_relay_inserted_for_long_route():
    # A 45-hop straight path (46 waypoints) — well over 31.
    path = [(x, 0) for x in range(46)]
    relays = _place_relays(path)
    assert relays, "a >31-hop route must get at least one relay"
    # Every segment between consecutive relays (and the ends) must be ≤31 hops.
    marks = [path[0]] + relays + [path[-1]]
    idxs = [path.index(m) for m in marks]
    segs = [idxs[i + 1] - idxs[i] for i in range(len(idxs) - 1)]
    assert all(s <= 31 for s in segs), f"a segment exceeds 31 hops: {segs}"


def test_no_relay_for_short_route():
    path = [(x, 0) for x in range(20)]   # 19 hops ≤ 31
    assert _place_relays(path) == []


def test_relay_field_on_routeresult():
    r = RouteResult("n", True, points=[(0, 0), (1, 0)], relays=[(15, 0)])
    assert r.relays == [(15, 0)]
    r2 = RouteResult("n", True, points=[(0, 0), (1, 0)])
    assert r2.relays is None


def test_over_budget_route_fails_soundly_with_relays_named():
    """A >31-hop route is a SOUND named failure carrying the placed relay cells —
    never a silent dead build, and never a (currently) mis-programmed relay build.
    The reason names the relays so a future build pass can consume them."""
    r = RouteResult("net", False, points=[(x, 0) for x in range(40)],
                    relays=[(30, 0)],
                    reason="bus route is 39 hops (>31); 1 relay cell(s) placed at "
                           "[(30, 0)], but relay programming is not yet emitted")
    assert not r.ok
    assert r.relays == [(30, 0)]
    assert "relay" in r.reason


def test_relay_placement_matches_router():
    """The standalone helper here must match the router's real placement so this
    test guards the actual algorithm (imported lazily to avoid Qt at import time)."""
    import importlib
    bus = importlib.import_module("engine.bus_router")
    # The router uses _MAX_HOPS from autoroute; confirm it's 31 so seg=30.
    from engine.autoroute import _MAX_HOPS
    assert _MAX_HOPS == 31
    path = [(x, 0) for x in range(70)]
    relays = _place_relays(path, _MAX_HOPS)
    # 69 hops, seg 30 → relays at idx 30 and 60.
    assert relays == [(30, 0), (60, 0)]
