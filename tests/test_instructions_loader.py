from ziva_runtime.config.instructions import load_layered_instructions


def test_no_instruction_files(tmp_path):
    result = load_layered_instructions(tmp_path)
    assert result == ""


def test_workspace_ziva_agents_md_loaded(tmp_path):
    agents = tmp_path / ".ziva" / "AGENTS.md"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text("Use Python 3.11\nFollow PEP 8")
    result = load_layered_instructions(tmp_path)
    assert "Python 3.11" in result
    assert "Follow PEP 8" in result


def test_project_root_agents_md_not_loaded(tmp_path):
    # Project-root AGENTS.md is intentionally NOT read — a workspace's
    # instructions live under `.ziva/`.
    (tmp_path / "AGENTS.md").write_text("project-root-should-not-load")
    assert load_layered_instructions(tmp_path) == ""


def test_claude_md_not_loaded(tmp_path):
    # CLAUDE.md is intentionally NOT read.
    (tmp_path / "CLAUDE.md").write_text("claude-should-not-load")
    assert load_layered_instructions(tmp_path) == ""


def test_workspace_ziva_takes_precedence_over_project_root(tmp_path):
    # If both exist, only the `.ziva/AGENTS.md` is read; the project-root
    # AGENTS.md must not bleed in.
    (tmp_path / "AGENTS.md").write_text("WRONG project root")
    agents = tmp_path / ".ziva" / "AGENTS.md"
    agents.parent.mkdir(parents=True, exist_ok=True)
    agents.write_text("RIGHT ziva")
    result = load_layered_instructions(tmp_path)
    assert "RIGHT ziva" in result
    assert "WRONG project root" not in result


def test_instructions_identical_across_workspaces_with_same_ziva_agents(tmp_path):
    # Two DIFFERENT workspaces, each with identical `.ziva/AGENTS.md`.
    # The instruction portion (global + workspace .ziva) must be IDENTICAL —
    # the workspace PATH does not leak into it (cwd lives in env_context).
    ws_a = tmp_path / "project-a"
    ws_b = tmp_path / "project-b"
    for ws in (ws_a, ws_b):
        ws.mkdir()
        (ws / ".ziva").mkdir()
        (ws / ".ziva" / "AGENTS.md").write_text("Shared workspace rule")
    assert load_layered_instructions(ws_a) == load_layered_instructions(ws_b)


def test_instructions_identical_across_workspaces_with_no_ziva_agents(tmp_path):
    # Neither workspace has a `.ziva/AGENTS.md` → instruction portion is
    # identical (both reduce to just the global file, if any).
    ws_a = tmp_path / "project-a"; ws_a.mkdir()
    ws_b = tmp_path / "project-b"; ws_b.mkdir()
    assert load_layered_instructions(ws_a) == load_layered_instructions(ws_b)


def test_instructions_differ_only_by_workspace_ziva_agents(tmp_path):
    # When the two workspaces' `.ziva/AGENTS.md` differ, the ONLY difference
    # in the instruction portion is that workspace's content.
    ws_a = tmp_path / "project-a"; ws_a.mkdir()
    ws_b = tmp_path / "project-b"; ws_b.mkdir()
    (ws_a / ".ziva").mkdir(); (ws_a / ".ziva" / "AGENTS.md").write_text("Rule A")
    (ws_b / ".ziva").mkdir(); (ws_b / ".ziva" / "AGENTS.md").write_text("Rule B")
    a = load_layered_instructions(ws_a)
    b = load_layered_instructions(ws_b)
    assert "Rule A" in a and "Rule A" not in b
    assert "Rule B" in b and "Rule B" not in a

