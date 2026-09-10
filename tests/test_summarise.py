import math
from pathlib import Path

import pytest

from flowdiff import summarise


@pytest.fixture(autouse=True)
def no_project_summariser():
    summarise._project = None
    summarise._prefixes = []
    yield
    summarise._project = None
    summarise._prefixes = []


def test_strings_under_the_tree_lose_the_tree_prefix():
    summarise.load_project_summariser("/t")
    assert summarise.summarise("/t/pkg/m.py") == "<tree>/pkg/m.py"
    assert summarise.summarise(["/t/pkg/m.py", "/elsewhere/m.py"]) == ["<tree>/pkg/m.py", "/elsewhere/m.py"]
    assert summarise.summarise("/t") == "/t" and summarise.summarise("/tx/m.py") == "/tx/m.py"
    assert summarise.summarise("/t/" + "a" * 200)["len"] == len("<tree>/") + 200


def test_scratch_prefix_wins_over_the_tree_it_sits_in():
    summarise.load_project_summariser("/t", "/t/.flowdiff")
    assert summarise.summarise("/t/.flowdiff/run/basetemp/x") == "<scratch>/run/basetemp/x"
    assert summarise.summarise("/t/lib.py") == "<tree>/lib.py"
    summarise.load_project_summariser("/t/.flowdiff/base", "/t/.flowdiff")
    assert summarise.summarise("/t/.flowdiff/run/basetemp/x") == "<scratch>/run/basetemp/x"
    assert summarise.summarise("/t/.flowdiff/base/lib.py") == "<tree>/lib.py"


def test_without_a_tree_strings_are_untouched():
    assert summarise.summarise("/t/m.py") == "/t/m.py"


def test_scalars_are_recorded_verbatim():
    assert [summarise.summarise(v) for v in (None, True, 7, 2.5, "hi")] == [None, True, 7, 2.5, "hi"]


def test_non_finite_floats_become_their_repr():
    assert summarise.summarise(math.inf) == "inf" and summarise.summarise(math.nan) == "nan"


def test_inline_limit_is_64_and_the_boundary_is_inclusive():
    assert summarise.INLINE_LIMIT == 64
    assert summarise.summarise("a" * 64) == "a" * 64
    long = summarise.summarise("a" * 65)
    assert long == {"type": "str", "len": 65, "sha256": summarise.digest(b"a" * 65)}
    assert len(long["sha256"]) == 16


def test_bytes_short_as_repr_long_as_digest():
    assert summarise.summarise(b"ab") == "b'ab'"
    assert summarise.summarise(bytearray(b"ab")) == "b'ab'"
    assert summarise.summarise(b"x" * 65) == {"type": "bytes", "len": 65, "sha256": summarise.digest(b"x" * 65)}


def test_small_sequences_recurse_and_large_ones_get_shape_hash_stats():
    assert summarise.summarise([1, (2, "s"), {3}]) == [1, [2, "s"], [3]]
    big = summarise.summarise(list(range(65)))
    assert big == {"type": "list", "len": 65, "sha256": summarise.digest(repr(list(range(65))).encode()),
                   "min": 0, "max": 64, "mean": 32.0}
    assert summarise.summarise(tuple(range(64))) == list(range(64))


def test_stats_only_when_every_element_is_a_number():
    assert summarise.stats([1, 2.0]) == {"min": 1, "max": 2.0, "mean": 1.5}
    assert summarise.stats([1, "x"]) == {} and summarise.stats([True, 1]) == {} and summarise.stats([]) == {}
    assert "min" not in summarise.summarise(["s"] * 65)


def test_dicts_stringify_keys_and_sets_are_sorted():
    assert summarise.summarise({1: "a", "b": None}) == {"1": "a", "b": None}
    assert summarise.summarise({3, 1, 2}) == [1, 2, 3]
    big = summarise.summarise({i: i for i in range(65)})
    assert big["type"] == "dict" and big["len"] == 65 and big["mean"] == 32.0


def test_nesting_stops_at_depth_four():
    nested = [[[[[1]]]]]
    assert summarise.MAX_DEPTH == 4
    assert summarise.summarise(nested) == [[[[{"type": "list"}]]]]


class Plain:
    def __init__(self):
        self.a = 1
        self.b = [2]


class Slotted:
    __slots__ = ("a",)

    def __init__(self):
        self.a = 1


def test_objects_expose_fields_or_only_their_type():
    assert summarise.summarise(Plain()) == {"type": "test_summarise.Plain", "fields": {"a": 1, "b": [2]}}
    assert summarise.summarise(Slotted()) == {"type": "test_summarise.Slotted"}
    assert summarise.type_name(1) == "int" and summarise.type_name(Plain()) == "test_summarise.Plain"


class FakeArray:
    shape = (2, 2)
    dtype = "float32"

    def __init__(self, numeric=True):
        self.numeric = numeric

    def tobytes(self):
        return b"\x00" * 16

    def min(self):
        if not self.numeric:
            raise TypeError("no ordering")
        return 0

    def max(self):
        return 3

    def mean(self):
        return 1.5


def test_array_likes_are_summarised_by_shape_dtype_hash_and_stats():
    assert summarise.summarise(FakeArray()) == {"type": "test_summarise.FakeArray", "shape": [2, 2],
                                                "dtype": "float32", "sha256": summarise.digest(b"\x00" * 16),
                                                "min": 0.0, "max": 3.0, "mean": 1.5}
    assert "min" not in summarise.summarise(FakeArray(numeric=False))


def test_project_summariser_takes_precedence_and_can_decline(tmp_path: Path):
    (tmp_path / ".flowdiff").mkdir()
    (tmp_path / ".flowdiff" / "summarisers.py").write_text(
        "def summarise(type_name, value):\n"
        "    return {'plain': value.a} if type_name.endswith('Plain') else None\n")
    summarise.load_project_summariser(str(tmp_path))
    assert summarise.summarise(Plain()) == {"plain": 1}
    assert summarise.summarise(5) == 5


def test_missing_project_summariser_resets_to_none(tmp_path: Path):
    summarise._project = lambda t, v: {"stale": True}
    summarise.load_project_summariser(str(tmp_path))
    assert summarise._project is None and summarise.summarise(1) == 1
