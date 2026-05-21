"""ensemble runs list/show/compare/export against a synthesized index."""

import json
from pathlib import Path

from ensemble.cli_runs import main


def _write_index(traces_dir: Path, rows):
    traces_dir.mkdir(parents=True, exist_ok=True)
    with (traces_dir / "runs.jsonl").open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_list_prints_runs(tmp_path: Path, capsys):
    _write_index(
        tmp_path,
        [
            {
                "run_id": "20260101T120000_foo_aaaaaaaa",
                "scenario": "foo.smoke",
                "finished_at": 1735732800,
                "scores": {"ok": 1.0},
            },
            {
                "run_id": "20260102T120000_foo_bbbbbbbb",
                "scenario": "foo.smoke",
                "finished_at": 1735819200,
                "scores": {"ok": 0.5},
            },
        ],
    )
    rc = main(["--traces-dir", str(tmp_path), "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "20260101T120000_foo_aaaaaaaa" in out
    assert "20260102T120000_foo_bbbbbbbb" in out
    assert "ok=1.0" in out


def test_show_resolves_prefix(tmp_path: Path, capsys):
    _write_index(
        tmp_path,
        [{"run_id": "20260101T120000_x_aaaaaaaa", "scenario": "x", "scores": {"ok": 1.0}}],
    )
    rc = main(["--traces-dir", str(tmp_path), "show", "20260101"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["run_id"] == "20260101T120000_x_aaaaaaaa"


def test_compare_diffs_scores(tmp_path: Path, capsys):
    _write_index(
        tmp_path,
        [
            {"run_id": "aaa", "scenario": "x", "scores": {"ok": 1.0, "speed": 0.8}},
            {"run_id": "bbb", "scenario": "x", "scores": {"ok": 0.5, "speed": 0.9}},
        ],
    )
    rc = main(["--traces-dir", str(tmp_path), "compare", "aaa", "bbb"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok" in out
    assert "speed" in out
    assert "-0.500" in out
    assert "+0.100" in out


def test_export_csv(tmp_path: Path, capsys):
    _write_index(
        tmp_path,
        [{"run_id": "aaa", "scenario": "x", "world": "x", "scores": {"ok": 1.0}}],
    )
    rc = main(["--traces-dir", str(tmp_path), "export", "--format", "csv"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run_id,scenario,world" in out
    assert "score.ok" in out
    assert "1.0" in out
