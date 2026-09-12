from pathlib import Path

from tools.mutation_gate import SCORE_EXEMPT, Result


def result(module: str, killed: int, survived: int) -> Result:
    return Result(module, {"killed": killed, "survived": survived}, "", 1.0)


def test_protocol_module_below_the_floor_is_not_gated_on_its_score() -> None:
    assert result("flowdiff/lsp.py", 1, 3).score() == 25.0
    assert result("flowdiff/lsp.py", 1, 3).passed(80.0)


def test_protocol_module_with_no_mutants_is_still_a_broken_gate() -> None:
    assert not result("flowdiff/lsp.py", 0, 0).passed(80.0)


def test_raising_the_threshold_does_not_re_gate_a_protocol_module() -> None:
    assert result("flowdiff/container.py", 83, 17).passed(95.0)


def test_logic_module_below_the_floor_still_fails() -> None:
    assert not result("flowdiff/graph.py", 79, 21).passed(80.0)


def test_exempt_line_reports_the_measured_score_beside_the_verdict() -> None:
    line = result("flowdiff/harness_cpp.py", 858, 142).line(80.0)
    assert "85.8%" in line
    assert "exempt (#34)" in line
    assert "BELOW THRESHOLD" not in line


def test_every_exempt_module_still_exists() -> None:
    assert all(Path(module).is_file() for module in SCORE_EXEMPT)
