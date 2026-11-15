import pytest

from radfetch.flows import FlowError, load_flow
from radfetch.gateway.pacs import SeriesInfo


def series(uid, modality, description, n):
    return SeriesInfo(uid, "1.2", modality, description, n)


ALL = [
    series("a", "CT", "Thorax 2.0", 120),
    series("b", "CT", "THORAX lung", 300),
    series("c", "CT", "Scout", 2),
    series("d", "MR", "Thorax", 50),
    series("e", "CT", "thorax unknown count", None),
]


def test_select(settings):
    flow = load_flow(settings.flows_dir, "seg")
    assert [s.series_instance_uid for s in flow.select_series(ALL)] == ["a", "b", "e"]
    assert flow.version == "3" and flow.gpu


def test_pick_largest(settings):
    (settings.flows_dir / "big.yaml").write_text(
        "name: big\npick: largest\nimage: x\nselect:\n  - modality: CT\n"
    )
    assert [s.series_instance_uid for s in load_flow(settings.flows_dir, "big").select_series(ALL)] == ["b"]


def test_errors(settings):
    with pytest.raises(FlowError, match="unknown flow"):
        load_flow(settings.flows_dir, "nope")
    with pytest.raises(FlowError, match="invalid flow name"):
        load_flow(settings.flows_dir, "../seg")
    (settings.flows_dir / "bad.yaml").write_text("name: bad\nimage: x\nselect: []\n")
    with pytest.raises(FlowError, match="invalid flow file"):
        load_flow(settings.flows_dir, "bad")
    (settings.flows_dir / "other.yaml").write_text("name: different\nimage: x\nselect:\n  - modality: CT\n")
    with pytest.raises(FlowError, match="file stem"):
        load_flow(settings.flows_dir, "other")
