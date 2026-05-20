"""world.toml manifest parsing."""

import pytest

from ensemble import ManifestError, load_world_manifest


def test_plank_manifest_parses():
    m = load_world_manifest("examples/plank")
    assert m.name == "plank"
    assert m.python_package == "plank"
    assert m.rust_crate == "world"
    assert m.personas_dir is not None and m.personas_dir.name == "personas"
    assert "frustrated_power_user" in m.default_personas
    assert "open_ticket" in m.default_tools
    assert "issue_refund" in m.default_tools


def test_missing_world_table_errors(tmp_path):
    p = tmp_path / "world.toml"
    p.write_text("[other]\nname = 'x'\n")
    with pytest.raises(ManifestError, match="missing required"):
        load_world_manifest(p)


def test_missing_name_errors(tmp_path):
    p = tmp_path / "world.toml"
    p.write_text("[world]\npython_package = 'foo'\n")
    with pytest.raises(ManifestError, match="world.name is required"):
        load_world_manifest(p)


def test_directory_argument_finds_manifest(tmp_path):
    p = tmp_path / "world.toml"
    p.write_text("[world]\nname = 'demo'\n")
    m = load_world_manifest(tmp_path)
    assert m.name == "demo"
    assert m.python_package == "demo"  # falls back to name


def test_invalid_toml_errors(tmp_path):
    p = tmp_path / "world.toml"
    p.write_text("this is = not toml [[")
    with pytest.raises(ManifestError, match="invalid TOML"):
        load_world_manifest(p)
