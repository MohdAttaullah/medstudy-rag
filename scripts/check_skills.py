"""Check equivalent project skills without requiring a particular coding agent."""

from pathlib import Path

import yaml

root = Path(__file__).resolve().parents[1]
codex = root / ".agents/skills"
claude = root / ".claude/skills"
expected = {
    "architecture-guardian",
    "medical-grounding",
    "document-ingestion",
    "retrieval-quality",
    "rag-evaluation",
    "frontend-quality",
    "security-review",
    "devops-observability",
    "handoff-maintainer",
}
for directory in (codex, claude):
    assert {path.parent.name for path in directory.glob("*/SKILL.md")} == expected
for name in sorted(expected):
    left = (codex / name / "SKILL.md").read_text(encoding="utf-8")
    right = (claude / name / "SKILL.md").read_text(encoding="utf-8")
    assert left == right, f"Skill drift: {name}"
    metadata = yaml.safe_load(left.split("---", 2)[1])
    assert metadata["name"] == name and metadata["description"]
print(f"PASS: {len(expected)} synchronized skill pairs with valid frontmatter")
