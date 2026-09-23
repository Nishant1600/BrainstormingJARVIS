# Widget Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every construct below was taken from real plans written by the
superpowers skills — heading depths, separators, emphasis, preamble bullets and
the fenced block that quotes the brief back at itself. It is a fixture so that
the parser is exercised against plan syntax as it is actually emitted, not as a
test would invent it.

**Tech Stack:** Python stdlib. No new dependencies.

## Global Constraints

These are bullets, not checkboxes, and they sit above the first task heading.
Nothing here belongs to a task and none of it may be counted as a step.

- No new Python or npm dependencies.
- Test command: `pytest tests/ -q`.
- Never `git push`.

## File Structure

| file | responsibility |
|---|---|
| `widget.py` (new) | The sync itself. Pure; no server imports. |
| `tests/test_widget.py` (new) | Round trip, conflict, and failure. |

---

## Task 1: The store

**Files:**
- Create: `widget.py`

- [x] Write the failing test for a round trip
- [x] Implement `load()` and `save()`
- [ ] Handle a corrupt file without raising

## Task 2: **Conflict resolution**

Emphasis in a task title is stripped by the parser, so this heading's title is
`Conflict resolution` with no asterisks.

- [x] Detect a conflicting write
- [ ] Resolve last-write-wins
- [ ] Record the loser in the log

### Task 3 — Deeper heading, em-dash separator

Real plans use `##` and `###` interchangeably and separate the number from the
title with a colon, a dash or an em dash.

- [ ] Accept a third depth
- [ ] Accept the em dash

## Verification

A non-task `##` heading between tasks. The parser ignores it, and the steps
below still belong to Task 3 because no new task heading has opened.

- [ ] Run the suite

## Task 4: Quoting the brief

Real plans quote the build brief inside a fenced block, and the brief itself is
written in checkboxes. The parser has no fence handling, so these count — that
is the measured behaviour of every real plan in this format, not an accident of
this fixture.

```
- [ ] Read the settled spec
- [ ] Write the plan, then review it against the spec
```

- [ ] Leave the fence alone
