from scheduling.bundle_builder import build_bundles
from tests.conftest import req


def test_321_bundle_structure():
    bundles = build_bundles([req(1, 1, 1, 1, 3, 0), req(2, 1, 2, 2, 2, 1), req(3, 1, 3, 3, 1, 2)])
    assert [[item.requirement_id for item in bundle.items] for bundle in bundles] == [[1, 2, 3], [1, 2], [1]]


def test_331_bundle_structure():
    bundles = build_bundles([
        req(1, 1, 1, 1, 3, 0),
        req(2, 1, 2, 2, 3, 1),
        req(3, 1, 3, 3, 1, 2),
    ])
    assert [[item.requirement_id for item in bundle.items] for bundle in bundles] == [
        [1, 2, 3],
        [1, 2],
        [1, 2],
    ]


def test_2211_bundle_structure():
    bundles = build_bundles([
        req(1, 1, 1, 1, 2, 0), req(2, 1, 2, 2, 2, 1),
        req(3, 1, 3, 3, 1, 2), req(4, 1, 4, 4, 1, 3),
    ])
    assert [len(bundle.items) for bundle in bundles] == [4, 2]


def test_532_and_single_bundle_structure():
    bundles = build_bundles([req(1, 1, 1, 1, 5, 0), req(2, 1, 2, 2, 3, 1), req(3, 1, 3, 3, 2, 2)])
    assert [len(bundle.items) for bundle in bundles] == [3, 3, 2, 1, 1]
    assert len(build_bundles([req(4, 2, 1, 1, 1)])) == 1
