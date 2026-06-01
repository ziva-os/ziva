from ziva_runtime.config.instructions import load_layered_instructions


def test_no_instruction_files(tmp_path):
    result = load_layered_instructions(tmp_path)
    assert result == ""


def test_project_agents_md(tmp_path):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("Use Python 3.11\nFollow PEP 8")
    result = load_layered_instructions(tmp_path)
    assert "Python 3.11" in result
    assert "project" in result


def test_claude_md_compat(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("Use TypeScript strict mode")
    result = load_layered_instructions(tmp_path)
    assert "TypeScript" in result
    assert "claude" in result


def test_layered_merge(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Project rule")
    (tmp_path / "CLAUDE.md").write_text("Claude rule")
    result = load_layered_instructions(tmp_path)
    assert "Project rule" in result
    assert "Claude rule" in result
