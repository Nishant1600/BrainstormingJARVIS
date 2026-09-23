#!/usr/bin/env python3
"""Render the animated dashboard walkthrough at the top of the README.

    docs/images/dashboard-walkthrough.gif

REGENERATE THIS WHENEVER THE DASHBOARD UI CHANGES. Every frame is a live
render of `frontend/dashboard-preview.html` through the stylesheet that
ships, so the GIF goes stale the moment that page or `theme/*.css` moves.

    .venv/bin/python scripts/make_dashboard_walkthrough.py

Needs Playwright's Chromium (already in .venv) and `ffmpeg` on PATH. It adds
no dependency: Playwright renders the frames, ffmpeg's palettegen/paletteuse
assembles the GIF. `--frames-only` stops after writing one PNG per beat,
which is how you check a beat by eye before spending the encode. LOOK AT THE
FRAMES. Two layout problems and one privacy problem in this file were found
that way and could not have been found by reading.

WHAT THIS SCRIPT IS ALLOWED TO DO TO THE PAGE
---------------------------------------------
Nothing is composited, drawn on, or retouched. Chromium renders the preview
page and the screenshot is the frame. The only things done to the page are:

1. Hide the preview page's own non-shipping chrome — `.pv-head`, `.pv-note`,
   `.pv-cap`, `.pv-foot`, and its standalone `.banner` demo — plus every
   example except the one on stage, so a frame holds one real view and
   nothing else. This is the crop the three still screenshots take,
   expressed in CSS instead of a clip box.
2. Move `active` / `aria-selected` along the real tab strip, and move the
   real mouse so real `:hover` rules fire. Both are states the shipping CSS
   defines; the preview page carries no JS to trigger them. There is no
   drawn cursor anywhere in the GIF — a painted-on pointer would be the one
   thing on screen that is not a render of this codebase.
3. Put real markup into the real container it ships in: swapping the two
   session detail fragments through the one `.split-detail` column, which is
   exactly what `sessions.ts` does when a row is clicked.

No view is invented, no number is edited, and no state is assembled that the
app cannot reach on its own.

TWO THINGS THIS SCRIPT DELIBERATELY WILL NOT FILM
-------------------------------------------------
* THE MEMORY VIEW. The preview page's Memory section is seeded with real
  entries out of the author's own memory store — real titles, real measured
  latencies. It must never be filmed.
* THE USAGE "OVERALL" PANEL, and everything below it. Commit 1dec0d4
  replaced the author's real measured usage figures with invented ones, but
  it missed some: the Overall panel's "38 Projects" is the real count named
  in that commit message, and its token split (16.6B cache read against a
  4.2B all-time total) does not reconcile with the replaced figures, which
  is what a partial scrub looks like. The Usage beat therefore shows the
  Subscription panel only — the same cut as `docs/images/dashboard-usage.png`.
If you add scenes, read the markup behind them before you ship the frame.

ONE PLACE THE PREVIEW PAGE HAS DRIFTED
--------------------------------------
`frontend/dashboard.html` wraps Specs and Projects in the shared `.split`
primitive (`.split > .split-master + .split-detail`). The preview page names
those wrappers `.specs-split` and `.projects-layout`, which no stylesheet
defines — so it renders both views as full-width stacks rather than the
master-detail they ship as. This script films them as the preview page
writes them, because reconstructing `.split` here would be inventing a
layout rather than recording one. Worth knowing before you trust those two
beats as pixel-accurate: they are accurate to the preview page, and the
preview page is behind the app. (Applying `.split` by hand also shows why
nobody has noticed: at the master column's 340px cap the row grid collapses
`.row-main` to zero width, so project names render as "c.." and a spec row
loses its title entirely. Fix that first, then the wrapper names, then
re-run this.)
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
PAGE = ROOT / "frontend" / "dashboard-preview.html"
OUT = ROOT / "docs" / "images" / "dashboard-walkthrough.gif"

# 1100 wide renders at ~900px in a GitHub README. The height is one browser
# window, so a tall view is cropped by the window exactly as a real one would
# be. 1x DPR: a 112-colour GIF palette cannot pay for retina text.
WIDTH, HEIGHT = 1100, 760
FPS = 10

# Somewhere in the page gutter, hovering nothing.
NEUTRAL_MOUSE = (3, 420)

TAB_INDEX = {"Runs": 1, "Sessions": 2, "Memory": 3, "Specs": 4, "Projects": 5, "Usage": 6}
TAB = ".shell-tabs .tab:nth-child(%d)"


def secs(n: float) -> int:
    return max(1, round(n * FPS))


# The real Projects view is one list and one detail. The preview stacks three
# detail states in that column so they can be compared; keep the first.
ONE_PROJECT_DETAIL = """
  [...document.querySelectorAll('.projects-layout > .proj-detail')]
    .slice(1).forEach(d => d.classList.add('stage-off'));
"""

# Nothing selected: the "At a Glance" tally sessions.ts renders into the
# detail column when no row is open, put back into that column.
NOTHING_SELECTED = """
  const slot = document.querySelector('.split.split--aside .split-detail');
  const tally = [...document.querySelectorAll('.pv-aside > .sd')].find(
    sd => sd.querySelector('.panel-title')?.textContent.trim() === 'At a Glance');
  slot.replaceChildren(tally.cloneNode(true));
"""

RUNS = "Runs, as it ships"
DETAIL = "Detail pane"
SESSIONS = "Sessions"
SPECS = "Specs — the review surface"
PROJECTS = "Projects"
USAGE = "Usage — what is left, and who spent it"

# A beat is: which section is on stage, which of its children survive, which
# tab is lit, what the mouse is over, where the page is scrolled, how long it
# holds. A "reach for the tab" beat holds the OUTGOING view and only lights
# the hover on the next tab, so the cut that follows reads as the click.
SCENES = [
    dict(name="runs", tab="Runs", section=RUNS, keep=[2], hold=secs(2.2)),
    dict(name="runs-hover", tab="Runs", section=RUNS, keep=[2],
         hover=".panel--alert .row--attn", hold=secs(0.7)),
    # The run detail, as the preview page presents it (`.pv-pane`, unpinned).
    # `.pane` asks for position:fixed but `.brackets`, loaded later, resets it
    # to relative — so in the app today this is an in-flow block too.
    dict(name="run-detail", tab="Runs", section=DETAIL, keep=[2], hold=secs(2.2)),
    dict(name="to-sessions", tab="Runs", section=DETAIL, keep=[2],
         hover=TAB % 2, hold=secs(0.6)),

    dict(name="sessions", tab="Sessions", section=SESSIONS, keep=[4],
         js=NOTHING_SELECTED, hold=secs(1.4)),
    dict(name="sessions-hover", tab="Sessions", section=SESSIONS, keep=[4],
         js=NOTHING_SELECTED, hover=".split-master .panel--alert .sess-row.is-open",
         hold=secs(0.6)),
    dict(name="session-open", tab="Sessions", section=SESSIONS, keep=[4], hold=secs(2.6)),
    dict(name="to-specs", tab="Sessions", section=SESSIONS, keep=[4],
         hover=TAB % 4, hold=secs(0.6)),

    dict(name="specs", tab="Specs", section=SPECS, keep=[2], hold=secs(2.2)),
    # The document column scrolls inside itself (`.specs-doc` is capped at
    # 100vh-220px and overflows), so the index beside it does not move — which
    # is the point of the arrangement.
    dict(name="specs-doc", tab="Specs", section=SPECS, keep=[2],
         scroll_in=(".specs-doc", 330), hold=secs(2.2)),
    dict(name="to-projects", tab="Specs", section=SPECS, keep=[2],
         scroll_in=(".specs-doc", 330), hover=TAB % 5, hold=secs(0.6)),

    dict(name="projects", tab="Projects", section=PROJECTS, keep=[2],
         js=ONE_PROJECT_DETAIL, hold=secs(2.0)),
    dict(name="project-detail", tab="Projects", section=PROJECTS, keep=[2],
         js=ONE_PROJECT_DETAIL, scroll=300, hold=secs(2.0)),
    dict(name="to-usage", tab="Projects", section=PROJECTS, keep=[2],
         js=ONE_PROJECT_DETAIL, scroll=300, hover=TAB % 6, hold=secs(0.6)),

    # Subscription panel only. See the header: the panels below it still carry
    # figures off the author's machine.
    dict(name="usage", tab="Usage", section=USAGE, keep=[3], hold=secs(2.2)),
    # Closes the loop: the hand goes back to Runs, and the GIF restarts on the
    # frame it opened with, so the repeat reads as one continuous session.
    dict(name="to-runs", tab="Usage", section=USAGE, keep=[3],
         hover=TAB % 1, hold=secs(0.6)),
]

STAGE_CSS = """
  /* Preview-page annotations, and its standalone demo of the offline banner.
     None of it ships, so none of it is filmed. */
  .pv-head, .pv-note, .pv-cap, .pv-foot, body > .banner { display: none !important; }
  .stage-off { display: none !important; }
  /* The preview section's gutter stands in for the view's own top padding. */
  .pv-section { padding-top: var(--sp-5); }
  /* Breathing dots and bar sweeps would smear across GIF frames, and the app
     stops all of it under prefers-reduced-motion anyway. */
  *, *::before, *::after { animation: none !important; transition: none !important; }
"""

STAGE_JS = """
([sectionTitle, keep]) => {
  document.querySelectorAll('.stage-off').forEach(e => e.classList.remove('stage-off'));
  let target = null;
  for (const s of document.querySelectorAll('.pv-section')) {
    const title = s.querySelector('.pv-title')?.textContent.trim();
    if (title === sectionTitle) target = s; else s.classList.add('stage-off');
  }
  if (!target) throw new Error('no such section: ' + sectionTitle);
  [...target.children].forEach((kid, i) => {
    if (!keep.includes(i)) kid.classList.add('stage-off');
  });
}
"""

TAB_JS = """
(index) => {
  [...document.querySelectorAll('.shell-tabs .tab')].forEach((t, i) => {
    const on = i === index - 1;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
}
"""


async def render_frames(frames_dir: pathlib.Path) -> list[pathlib.Path]:
    from playwright.async_api import async_playwright

    shots: list[pathlib.Path] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(
            viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1
        )
        await page.goto(PAGE.as_uri())
        await page.wait_for_timeout(600)
        await page.add_style_tag(content=STAGE_CSS)
        # Keep an untouched copy of the sessions detail column so the beats
        # that swap it can put the shipped one back.
        await page.evaluate(
            """() => { window.__detail = document
                 .querySelector('.split.split--aside .split-detail').cloneNode(true); }"""
        )

        for n, scene in enumerate(SCENES):
            await page.evaluate(
                """() => { document.querySelector('.split.split--aside .split-detail')
                     .replaceWith(window.__detail.cloneNode(true)); }"""
            )
            await page.evaluate(STAGE_JS, [scene["section"], scene["keep"]])
            await page.evaluate(TAB_JS, TAB_INDEX[scene["tab"]])
            if scene.get("js"):
                await page.evaluate("() => {%s}" % scene["js"])

            await page.evaluate("y => window.scrollTo(0, y)", scene.get("scroll", 0))
            await page.evaluate(
                """([sel, y]) => {
                     if (!sel) return;
                     const e = document.querySelector(sel);
                     if (!e) throw new Error('no scroll container: ' + sel);
                     e.scrollTop = y;
                   }""",
                list(scene.get("scroll_in", (None, 0))),
            )
            await page.mouse.move(*NEUTRAL_MOUSE)
            if scene.get("hover"):
                box = await page.locator(scene["hover"]).first.bounding_box()
                if box is None:
                    raise SystemExit(f"{scene['name']}: {scene['hover']} has no box")
                await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await page.wait_for_timeout(120)

            still = frames_dir / f"beat-{n:02d}-{scene['name']}.png"
            await page.screenshot(path=str(still))
            shots.extend([still] * scene["hold"])

        await browser.close()
    return shots


def lay_out_sequence(shots: list[pathlib.Path], seq_dir: pathlib.Path) -> None:
    seq_dir.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(shots):
        shutil.copyfile(src, seq_dir / f"f{i:05d}.png")


def assemble(seq_dir: pathlib.Path, out: pathlib.Path) -> None:
    """Two passes over one input: build one palette for the whole run, then
    map every frame onto it. stats_mode=diff weights the palette towards the
    pixels that actually change; diff_mode=rectangle lets the encoder write
    only the changed rectangle of each frame, which is what keeps twenty-odd
    seconds of a mostly-static page inside a couple of megabytes."""
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", str(FPS),
            "-i", str(seq_dir / "f%05d.png"),
            "-filter_complex",
            "[0:v]split[a][b];"
            "[a]palettegen=max_colors=112:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle:new=0",
            "-loop", "0",
            str(out),
        ],
        check=True,
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=OUT)
    ap.add_argument("--frames-only", action="store_true")
    ap.add_argument("--frames-dir", type=pathlib.Path, default=None)
    args = ap.parse_args()

    if not args.frames_only and not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not on PATH")

    tmp = args.frames_dir or pathlib.Path(tempfile.mkdtemp(prefix="jarvis-walkthrough-"))
    tmp.mkdir(parents=True, exist_ok=True)
    print(f"frames -> {tmp}")

    shots = await render_frames(tmp)
    print(f"{len(SCENES)} beats, {len(shots)} frames ({len(shots) / FPS:.1f}s at {FPS}fps)")
    if args.frames_only:
        return

    seq = tmp / "seq"
    lay_out_sequence(shots, seq)
    assemble(seq, args.out)
    size = args.out.stat().st_size
    print(f"{args.out} — {size / 1024:.0f} KiB, {WIDTH}x{HEIGHT}")
    if size > 3 * 1024 * 1024:
        print("WARNING: over 3 MiB. Drop beats or colours.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
