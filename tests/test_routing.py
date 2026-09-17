from pathlib import Path

from above_all.routing import load_routing, route

CONFIG = Path(__file__).parents[1] / "config" / "routing.example.toml"


def test_mechanical_uses_free():
    assert route(load_routing(CONFIG),"mechanical").tier == "free"


def test_judgment_uses_frontier():
    assert route(load_routing(CONFIG),"judgment").tier == "frontier"


def test_signal_escalates_free_step():
    assert route(load_routing(CONFIG),"routine",{"ambiguity_high"}).tier == "frontier"
