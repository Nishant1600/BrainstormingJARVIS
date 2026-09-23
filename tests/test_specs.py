"""The review surface: numbered sections, recorded approval, both states.

`specs.py` is the producer for the SPECS tab and for the two tools JARVIS
answers with. Four properties are load-bearing, and each has a test that
fails if it is removed:

1. **One numbering, two readers.** The page and JARVIS both get their section
   numbers from `specs.sections_of`. If the page ever computed its own, "drop
   five" would mean two different things on the two sides. Delete the shared
   call and `test_the_page_and_jarvis_are_handed_the_same_numbering` fails.
2. **Approval is a file.** A restart cannot forget that the user said yes, and
   a revision cannot inherit the yes the old text was given.
3. **Progress comes off the plan's checkboxes**, through `builds.parse_plan` —
   not re-implemented here.
4. **A path from a URL is attacker input.** Containment is decided by
   `repo_read.resolve_within`, and then narrowed again to the two document
   directories.

Nothing here spawns anything or writes outside tmp_path.
"""

import importlib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import builds  # noqa: E402
import specs  # noqa: E402
from tests.conftest import require_symlinks


# ---------------------------------------------------------------------------
# Fixtures — a project on disk with the artifacts builds.py actually produces
# ---------------------------------------------------------------------------

SPEC_BODY = """\
A local web UI to browse and edit CLAUDE.md files

A small local page that lists every CLAUDE.md on this machine and lets me edit
one in place. Read the files off disk, no database.
"""

PLAN = """\
# CLAUDE.md browser — Implementation Plan

A phased plan, written by the session from the settled design.

## Task 1: Read the files off disk

- [x] Walk the home directory for CLAUDE.md
- [x] Cap the walk at 20,000 entries

## Task 2: Serve the list

- [x] A JSON endpoint
- [ ] Sort by modification time

## Task 3: Edit in place

- [ ] A textarea
- [ ] A save button
"""


@pytest.fixture
def project(tmp_path):
    """A project with one spec and one plan, written the way builds.py does."""
    root = tmp_path / "claude-browser"
    root.mkdir()
    relative = builds.write_spec(str(root), SPEC_BODY)
    (root / builds.PLAN_DIR / "2026-09-03-claude-browser-plan.md").write_text(
        PLAN, encoding="utf-8")
    return root, relative


# ---------------------------------------------------------------------------
# 1. Numbering — the mechanism the whole surface rests on
# ---------------------------------------------------------------------------

def test_a_spec_numbers_its_top_level_sections_from_one():
    text = builds.render_spec(SPEC_BODY, constraints="No database.",
                              non_goals="No login.")
    sections = specs.sections_of(text)
    assert [(s.number, s.title) for s in sections] == [
        (1, "What we agreed"), (2, "Constraints"), (3, "Non-goals")]


def test_the_document_title_is_not_a_numbered_section():
    """A lone `#` at the top is the document's name, not its first section.

    Numbering it would make every spoken number one too high for the whole
    document — the exact failure this surface exists to prevent.
    """
    text = "# Runs Dashboard — Design\n\n## One\n\nbody\n\n## Two\n\nbody\n"
    assert [s.title for s in specs.sections_of(text)] == ["One", "Two"]


def test_sections_are_taken_from_the_shallowest_level_that_repeats():
    """A document with no title heading numbers its own top level."""
    text = "## Alpha\n\nbody\n\n## Beta\n\nbody\n"
    assert [(s.number, s.title) for s in specs.sections_of(text)] == [
        (1, "Alpha"), (2, "Beta")]


def test_a_plans_tasks_are_its_sections():
    sections = specs.sections_of(PLAN)
    assert [(s.number, s.title) for s in sections] == [
        (1, "Task 1: Read the files off disk"),
        (2, "Task 2: Serve the list"),
        (3, "Task 3: Edit in place")]


def test_a_heading_inside_a_code_fence_is_not_a_section():
    """Otherwise a spec that quotes a Markdown example renumbers itself, and
    every number the user says after that point lands on the wrong thing."""
    text = ("# Doc\n\n## One\n\n```markdown\n## Not a section\n# Nor this\n"
            "```\n\n## Two\n\nbody\n")
    assert [s.title for s in specs.sections_of(text)] == ["One", "Two"]


def test_a_section_carries_its_own_body_and_everything_nested_under_it():
    text = ("# Doc\n\n## One\n\nfirst\n\n### Deeper\n\nnested\n\n"
            "## Two\n\nsecond\n")
    one, two = specs.sections_of(text)
    assert "first" in one.body and "### Deeper" in one.body
    assert "second" not in one.body
    assert two.body.strip() == "second"


def test_text_before_the_first_section_is_a_preamble_with_no_number():
    text = "# Doc\n\nA sentence about the whole thing.\n\n## One\n\nbody\n"
    doc = specs.parse_document(text)
    assert "A sentence about the whole thing." in doc.preamble
    assert [s.number for s in doc.sections] == [1]


def test_a_document_with_no_headings_is_all_preamble():
    doc = specs.parse_document("just some prose\nover two lines\n")
    assert doc.sections == []
    assert doc.preamble.strip() == "just some prose\nover two lines"


def test_the_numbering_is_a_pure_function_of_the_document():
    """Same text in, same numbers out — no state, no ordering, no clock."""
    text = builds.render_spec(SPEC_BODY, constraints="No database.")
    once = [(s.number, s.title) for s in specs.sections_of(text)]
    twice = [(s.number, s.title) for s in specs.sections_of(text)]
    assert once == twice


def test_section_lookup_by_the_number_the_user_says():
    text = builds.render_spec(SPEC_BODY, constraints="No database.")
    assert specs.section_number(text, 2).title == "Constraints"
    assert specs.section_number(text, 9) is None
    assert specs.section_number(text, 0) is None


# ---------------------------------------------------------------------------
# 2. Approval — a real act, on disk, that a revision does not inherit
# ---------------------------------------------------------------------------

def test_a_document_nobody_approved_is_awaiting(project):
    root, relative = project
    assert specs.approval_of(str(root), relative)["state"] == "awaiting"


def test_approval_is_written_into_the_project_as_a_file(project):
    root, relative = project
    record = specs.record_approval(str(root), relative)

    path = root / specs.APPROVAL_DIR / specs.approval_filename(relative)
    assert path.is_file(), "approval must survive a restart, so it is a file"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["document"] == relative
    assert stored["digest"] == record["digest"]
    assert stored["approved_at"] > 0


def test_an_approved_document_reads_back_as_approved_from_disk(project):
    root, relative = project
    specs.record_approval(str(root), relative)
    # Reloaded module: nothing may be remembered in memory.
    importlib.reload(specs)
    state = specs.approval_of(str(root), relative)
    assert state["state"] == "approved"
    assert state["approved_at"] > 0


def test_a_revision_supersedes_the_approval_it_did_not_get(project):
    root, relative = project
    specs.record_approval(str(root), relative)
    doc = root / relative
    doc.write_text(doc.read_text(encoding="utf-8") + "\n## Late addition\n\nx\n",
                   encoding="utf-8")
    assert specs.approval_of(str(root), relative)["state"] == "superseded"


def test_re_approving_a_revision_clears_the_supersede(project):
    root, relative = project
    specs.record_approval(str(root), relative)
    doc = root / relative
    doc.write_text(doc.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    specs.record_approval(str(root), relative)
    assert specs.approval_of(str(root), relative)["state"] == "approved"


def test_an_approval_file_that_is_not_json_is_not_an_approval(project):
    """A corrupt record must read as "nobody approved this", never as a yes."""
    root, relative = project
    target = root / specs.APPROVAL_DIR / specs.approval_filename(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    assert specs.approval_of(str(root), relative)["state"] == "awaiting"


# ---------------------------------------------------------------------------
# 3. Both states — awaiting approval, and finished work awaiting review
# ---------------------------------------------------------------------------

def test_progress_comes_off_the_plans_checkboxes(project):
    root, _ = project
    docs = {d["path"]: d for d in specs.list_documents(str(root))}
    plan = next(d for d in docs.values() if d["kind"] == "plan")
    assert plan["progress"] == {
        "done": 1, "total": 3, "current": "Serve the list",
        "current_section": 2, "steps_done": 3, "steps_total": 6}


def test_a_spec_has_no_task_progress(project):
    root, relative = project
    spec = next(d for d in specs.list_documents(str(root))
                if d["path"] == relative)
    assert spec["kind"] == "spec"
    assert spec["progress"] is None


def test_an_unapproved_document_puts_the_project_in_awaiting_approval(project):
    root, _ = project
    assert specs.project_review(str(root))["state"] == "awaiting"


def test_an_approved_spec_with_a_plan_in_flight_reads_as_building(project):
    root, relative = project
    for d in specs.list_documents(str(root)):
        specs.record_approval(str(root), d["path"])
    review = specs.project_review(str(root))
    assert review["state"] == "building"
    assert review["progress"]["done"] == 1
    assert review["progress"]["total"] == 3


def test_a_finished_plan_reads_as_work_awaiting_review(project):
    root, _ = project
    plan = root / builds.PLAN_DIR / "2026-09-03-claude-browser-plan.md"
    plan.write_text(plan.read_text(encoding="utf-8").replace("- [ ]", "- [x]"),
                    encoding="utf-8")
    for d in specs.list_documents(str(root)):
        specs.record_approval(str(root), d["path"])
    review = specs.project_review(str(root))
    assert review["state"] == "review"
    assert review["progress"]["done"] == 3


def test_an_approved_spec_with_no_plan_yet_reads_as_planning(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    relative = builds.write_spec(str(root), SPEC_BODY)
    specs.record_approval(str(root), relative)
    assert specs.project_review(str(root))["state"] == "planning"


def test_a_project_with_no_documents_has_nothing_to_review(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    assert specs.list_documents(str(root)) == []
    assert specs.project_review(str(root)) is None


def test_documents_are_listed_newest_first(project):
    root, _ = project
    plan = root / builds.PLAN_DIR / "2026-09-03-claude-browser-plan.md"
    import os
    os.utime(plan, (time.time() + 10, time.time() + 10))
    listed = specs.list_documents(str(root))
    assert listed[0]["kind"] == "plan"


# ---------------------------------------------------------------------------
# 4. Paths out of a URL are attacker-shaped
# ---------------------------------------------------------------------------

def test_a_traversal_out_of_the_project_is_refused(project):
    root, _ = project
    assert specs.resolve_document(str(root), "../../../etc/passwd") is None
    assert specs.resolve_document(str(root), "/etc/passwd") is None


def test_a_file_outside_the_two_document_directories_is_refused(project):
    """A readable Markdown file, really there, inside the project — and still
    refused, because this surface reads specs and plans and nothing else."""
    root, _ = project
    (root / "NOTES.md").write_text("private", encoding="utf-8")
    (root / "docs" / "superpowers" / "scratch.md").write_text(
        "private", encoding="utf-8")
    assert (root / "NOTES.md").is_file()
    assert specs.resolve_document(str(root), "NOTES.md") is None
    assert specs.resolve_document(
        str(root), "docs/superpowers/scratch.md") is None


def test_a_symlink_pointing_out_of_the_project_is_refused(project):
    root, _ = project
    outside = root.parent / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = root / builds.SPEC_DIR / "linked.md"
    require_symlinks()
    link.symlink_to(outside)
    assert specs.resolve_document(str(root), str(link.relative_to(root))) is None


def test_a_real_document_resolves(project):
    root, relative = project
    resolved = specs.resolve_document(str(root), relative)
    assert resolved is not None and resolved.is_file()


def test_a_non_markdown_file_in_the_spec_directory_is_refused(project):
    root, _ = project
    (root / builds.SPEC_DIR / "notes.txt").write_text("x", encoding="utf-8")
    assert specs.resolve_document(
        str(root), f"{builds.SPEC_DIR}/notes.txt") is None


# ---------------------------------------------------------------------------
# 5. The whole document, as both readers receive it
# ---------------------------------------------------------------------------

def test_read_document_carries_sections_approval_and_progress(project):
    root, relative = project
    doc = specs.read_document(str(root), relative)
    assert doc["path"] == relative
    assert doc["kind"] == "spec"
    assert doc["approval"]["state"] == "awaiting"
    assert [s["number"] for s in doc["sections"]] == [1]
    assert doc["sections"][0]["title"] == "What we agreed"


def test_read_document_refuses_a_path_it_could_not_contain(project):
    root, _ = project
    assert specs.read_document(str(root), "../../../etc/passwd") is None


def test_the_outline_jarvis_hears_is_the_numbering_the_page_shows(project):
    """One function, two readers. This is the guarantee that "change three"
    means the same section to the user and to JARVIS."""
    root, relative = project
    page = specs.read_document(str(root), relative)
    spoken = specs.outline(str(root), relative)
    assert spoken == [(s["number"], s["title"]) for s in page["sections"]]
