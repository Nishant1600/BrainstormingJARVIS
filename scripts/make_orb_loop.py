#!/usr/bin/env python3
"""Render the seamlessly-looping orb at the top of the README.

    docs/images/orb-speaking.gif
    docs/images/orb-speaking.webp

REGENERATE THIS WHENEVER `frontend/src/orb.ts` CHANGES. Every pixel in both
files is a live WebGL render of that module — the real Three.js particle
system, in its real "speaking" state, with nothing composited, drawn on, or
retouched afterwards.

    .venv/bin/python scripts/make_orb_loop.py

Needs Playwright's Chromium (already in .venv), `ffmpeg` on PATH, and
`frontend/node_modules` installed (esbuild, which ships as a Vite dependency,
bundles the orb module). It adds no dependency of its own.

    --reference     re-take the real-AnalyserNode measurement below and stop
    --frames-only   render the PNG frames and stop, so you can look at them
    --seconds / --fps / --width / --height / --crossfade   as they say
    --scale         supersampling factor; 1 turns it off, and it shows

WHAT IS SYNTHETIC AND WHAT IS NOT
---------------------------------
The *input* is synthetic; the *output* is a real render.

`orb.ts` only knows it is talking through an `AnalyserNode`: each frame it
calls `getByteFrequencyData`, sums bins 0-7 into `bass` and bins 8-23 into
`mid`, and those two numbers drive the pulse (`mat.size + bass * 0.05`, the
opacity lift, the Z-breathing `- bass * 10`, the outward shove on `bass > 0.05`
and the per-particle syllable pulse on `mid > 0.1`). So this script hands the
orb an object shaped exactly like an `AnalyserNode` whose spectrum is a
speech-shaped envelope computed from the frame clock. Synthesising real TTS
would spend the user's Fish Audio quota and change nothing on screen.

The envelope is not guesswork, and it is not eyeballed off a video. It was
fitted to a measurement of a REAL `AnalyserNode`, configured exactly the way
`voice.ts` configures the one the app uses (fftSize 256, smoothingTimeConstant
0.8) and fed a 4.5 Hz-gated 120 Hz sawtooth through a formant lowpass at TTS
playback level, in the same headless Chromium. `--reference` takes that
measurement again — it is a WebAudio graph, it spends no quota and needs no
network. In steady state it gave the orb:

    in a phrase   bass 0.63 - 0.87 (mean 0.77)   mid 0.35 - 0.64 (mean 0.52)
    in the gap    bass -> 0.00                   mid -> 0.00
    a syllable is worth about +-0.1 of bass; the phrase gap is worth all of it

(That is one run of `--reference`. It samples a live audio graph off the wall
clock, so a repeat moves the ends of the ranges by a few hundredths; the middle
is stable.)

Two things worth knowing, because they decide what the loop should look like.
First, the analyser's own 0.8 smoothing eats most of the 4.5 Hz syllable, so
the big visible event in real speech is the PHRASE GAP, not the syllable.
Second, the byte spectrum is a decibel scale, so it rolls off almost linearly
across the bins rather than dying away — which is why `mid` sits at about 0.68
of `bass` rather than a tenth of it.

SPEECH_* below are fitted to reproduce all of that through the same smoothing
(bass 0.62-0.87 mean 0.77, mid 0.43-0.60 mean 0.53, floor 0.03), and every run
prints what it actually produced next to the measured numbers so the fit can be
checked rather than trusted. The one place this differs in kind from the real
node: the smoothing here is applied to the bytes rather than to the linear
magnitudes underneath them.

HOW THE LOOP IS MADE TO CLOSE
-----------------------------
Nothing in `orb.ts` has a period. The camera drifts on `sin(t*0.02)` and
`cos(t*0.03)`, the cloud breathes on `sin(t*0.15)` while speaking, and the
2000 particles are a damped random walk that never revisits a state. Three
things are done about that, in order of how much they buy:

1. A VIRTUAL CLOCK. The harness page replaces `performance.now` and
   `requestAnimationFrame` before the orb module loads, so every frame is
   advanced by hand at exactly 1/60 s — the rate the app runs at. The frame
   times are then exact rather than whatever the machine managed, which is what
   lets step 2 mean anything. `Math.random` is seeded in the same place and for
   a duller reason: the orb scatters its 2000 particles with it, so without a
   seed the file is different every time it is regenerated. Between them the
   whole render is reproducible — the same command gives the same bytes.

2. AN ENVELOPE WHOSE PERIOD IS THE LOOP. Every component of the synthetic
   speech is a whole number of cycles per loop (at the 6 s default: 3 phrases,
   27 syllables = 4.5 Hz, a 21-cycle flutter = 3.5 Hz, one slow swell). The
   script refuses to run if any of them is not. So the audio-driven half of the
   motion — the pulse, the size, the opacity, the Z lunge — is bit-identical at
   both ends of the loop.

3. A CROSSFADE over the half that cannot be made periodic. The clock does not
   start at 0: `best_start()` sweeps for the instant at which the camera and
   breathing sinusoids come closest to returning to themselves one loop later.
   At the 6 s default that is t = 531.6 s, and it leaves a seam worth about
   3.5 px of apparent motion where a naive start at t=0 would leave 15.1 px.
   The clock is simply jumped there during warm-up, before anything is filmed,
   so it costs nothing but a print. A 1 s crossfade buries what is left.

   The crossfade is the standard loop construction: capture N+K frames, then
   replace the first K with `a*frame[j] + (1-a)*frame[N+j]`, a ramping 0 to 1.
   Frame N+j is exactly one loop-period after frame j, so the two agree on
   everything in (2) and disagree only on the camera drift and the particle
   positions, which are a statistically identical cloud either way. The wrap
   itself (output frame N-1 back to output frame 0) is exactly continuous,
   because output frame 0 IS frame N.

   Verified, not assumed: the script prints the PSNR across the wrap next to
   the median PSNR between ordinary neighbouring frames. If the wrap is not
   worse than an ordinary frame step, there is no seam to find.

GIF, WEBP, AND WHY THERE IS NO BORDER
-------------------------------------
Both are written. The GIF is what the README uses, because it renders
everywhere, and at these settings it lands at 2.8 MiB — under the 3 MiB a
README should ask anyone to download, but only just. The WebP is the same 90
frames at 1.2 MiB and a visibly cleaner picture; swapping the README's one line
to it is a fair trade the day size matters more than compatibility does.

The orb page is `#050508`, so on GitHub's light theme this is a dark rectangle
on a white page. Nothing is padded, drawn, or matted around it: a border would
be the one thing in the file that is not a render of the orb. What keeps it
from reading as a hole is the framing — the glow runs most of the way to the
edges of a frame this size — and the caption sitting directly under it.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import math
import pathlib
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
ORB_TS = ROOT / "frontend" / "src" / "orb.ts"
ESBUILD = ROOT / "frontend" / "node_modules" / ".bin" / "esbuild"
OUT_GIF = ROOT / "docs" / "images" / "orb-speaking.gif"

# 600x380 is a browser window's aspect, near enough. The orb is always about
# 48% of the frame HEIGHT — three.js fixes the vertical field of view, so a
# shorter window shrinks the orb rather than cropping it — and the file size is
# set almost entirely by the orb's pixel area, because black margins cost LZW
# nothing (measured: cropping 760x480 down to 560x400 changed the GIF by 0 KiB,
# while scaling it did). Every pixel of the orb changes every frame, so none of
# the diff-mode tricks that keep the dashboard walkthrough small apply, and the
# GIF runs about 32 KiB a frame at this size. Hence 6 s at 15 fps: 90 frames,
# about 2.8 MiB. The WebP of the same frames is well under half of that.
WIDTH, HEIGHT = 600, 380
FPS = 15                # output frames per second
RENDER_HZ = 60          # orb frames per second, as the app runs; a multiple of FPS
SUPERSAMPLE = 2         # render at 2x and average down; see encode()
GIF_COLORS = 24         # one blue ramp on black — see encode()
LOOP_SECONDS = 6.0
CROSSFADE_SECONDS = 1.0

# The orb's per-frame lerps are 0.02, so ~200 frames converges any of them; the
# transition tumble decays by 0.985 a frame and needs about the same. Give it
# plenty, then jump the clock to best_start()'s instant and let the Z spring
# re-settle there.
WARMUP_FRAMES = 900
SETTLE_SECONDS = 4.0

# Synthetic speech envelope. Every rate here times LOOP_SECONDS must come out a
# whole number of cycles, or the envelope does not close the loop; the script
# checks. 4.5 Hz syllables also stay well clear of half the frame rate, so the
# pulse is sampled rather than strobed.
SYLLABLE_HZ = 4.5
PHRASE_HZ = 0.5
FLUTTER_HZ = 3.5
PHRASE_GAP = 0.20       # fraction of each phrase that is silence

# Synthetic speech spectrum, fitted to the measurement in the header. PEAK and
# TILT set the bass:mid ratio the orb reads; BASE and DEPTH set the loudness and
# how deep the syllables cut into it, chosen so that after the 0.8 smoothing the
# bass and mid land on the measured trace rather than near it.
SPEECH_PEAK = 248.0     # byte value of bin 0 at full loudness
SPEECH_TILT = 5.9       # bytes lost per bin going up the spectrum
SPEECH_BASE = 0.94
SPEECH_DEPTH = 0.13
SPEECH_SMOOTHING = 0.8  # AnalyserNode.smoothingTimeConstant, as voice.ts sets it


REFERENCE_JS = r"""
async () => {
  // A real AnalyserNode, set up exactly as voice.ts sets up the one the app
  // hands the orb, fed a voiced-speech stand-in: a 120 Hz glottal buzz through
  // a formant lowpass, gated into 4.5 Hz syllables and two phrases with real
  // silence between them, at the level TTS playback actually sits at.
  const ctx = new AudioContext();
  await ctx.resume();
  const an = ctx.createAnalyser();
  an.fftSize = 256; an.smoothingTimeConstant = 0.8;
  const osc = ctx.createOscillator(); osc.type = 'sawtooth'; osc.frequency.value = 120;
  const lp = ctx.createBiquadFilter();
  lp.type = 'lowpass'; lp.frequency.value = 2600; lp.Q.value = 1.2;
  const g = ctx.createGain(); g.gain.value = 0;
  osc.connect(lp); lp.connect(g); g.connect(an); an.connect(ctx.destination);
  const t0 = ctx.currentTime + 0.05;
  let t = t0;
  for (let phrase = 0; phrase < 2; phrase++) {
    for (let k = 0; k < 13; k++) {
      g.gain.setValueAtTime(0.05, t);
      g.gain.linearRampToValueAtTime(0.34, t + 0.075);
      g.gain.linearRampToValueAtTime(0.05, t + 0.19);
      t += 1 / 4.5;
    }
    g.gain.setValueAtTime(0, t);
    t += 0.6;
  }
  osc.start();
  const buf = new Uint8Array(an.frequencyBinCount);
  const rows = [];
  const wall = performance.now();
  while (performance.now() - wall < 7000) {
    await new Promise(r => setTimeout(r, 16));
    an.getByteFrequencyData(buf);          // the orb's own two sums
    let b = 0, m = 0;
    for (let i = 0; i < 8; i++) b += buf[i];
    for (let i = 8; i < 24; i++) m += buf[i];
    rows.push([b / (8 * 255), m / (16 * 255), ctx.currentTime - t0]);
  }
  osc.stop();
  return rows;
}
"""


async def take_reference() -> None:
    """Re-take the measurement the SPEECH_* constants are fitted to, and print
    it. The numbers in the header came from exactly this."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=["--autoplay-policy=no-user-gesture-required"])
        page = await browser.new_page()
        await page.goto("about:blank")
        rows = await page.evaluate(REFERENCE_JS)
        await browser.close()

    speech = [r for r in rows if 1.0 <= r[2] <= 2.8]      # mid-phrase
    gap = [r for r in rows if 3.05 <= r[2] <= 3.5]        # the silence after it
    if not speech or not gap:
        raise SystemExit("the audio graph did not run; nothing to measure")

    def stat(rows: list, i: int) -> str:
        v = [r[i] for r in rows]
        return f"{min(v):.2f}-{max(v):.2f} (mean {sum(v) / len(v):.2f})"

    print(f"in a phrase  bass {stat(speech, 0)}   mid {stat(speech, 1)}")
    print(f"in the gap   bass {stat(gap, 0)}   mid {stat(gap, 1)}")
    print("bass, one second mid-phrase:")
    print("  " + " ".join(f"{r[0]:.2f}" for r in rows if 1.5 <= r[2] <= 2.5))


def best_start(period: float, horizon: float = 700.0) -> tuple[float, float, float]:
    """The clock time at which the orb's un-loopable slow motions come closest
    to repeating one `period` later. Returns (start, its cost, the cost of a
    naive start at t=0), both costs in apparent pixels.

    Three sinusoids survive warm-up in the speaking state: the camera's
    `sin(t*0.02)*5` and `cos(t*0.03)*3`, and the Z-breathing's `sin(t*0.15)*6`.
    Their mismatch is weighted into apparent pixels — a world unit of camera
    pan is about 8.5 px at this framing, a world unit of Z about 2 px of edge
    motion — and sampled a second either side of the seam as well, because the
    Z spring has about that much memory.
    """
    def cost(t0: float) -> float:
        total = 0.0
        for off in (-1.0, -0.5, 0.0):
            a, b = t0 + off, t0 + period + off
            total += 8.5 * abs(5 * (math.sin(0.02 * b) - math.sin(0.02 * a)))
            total += 8.5 * abs(3 * (math.cos(0.03 * b) - math.cos(0.03 * a)))
            total += 2.0 * abs(6 * (math.sin(0.15 * b) - math.sin(0.15 * a)))
        return total / 3

    step = 0.01
    t0 = min((i * step for i in range(int(horizon / step))), key=cost)
    return t0, cost(t0), cost(0.0)


HARNESS = """<!doctype html>
<meta charset="utf-8">
<title>orb loop harness</title>
<style>html,body{margin:0;padding:0;background:#050508;overflow:hidden}
canvas{display:block}</style>
<script>
  // The virtual clock, installed before the orb module is imported so that
  // three's Clock and the animation loop see nothing else. Every frame is
  // advanced by hand: the render is deterministic and the frame times exact.
  window.__vt = 0;
  performance.now = function () { return window.__vt; };
  const __queue = [];
  window.requestAnimationFrame = function (cb) { __queue.push(cb); return __queue.length; };
  window.__advance = function (frames, dtMs) {
    for (let i = 0; i < frames; i++) {
      window.__vt += dtMs;
      const due = __queue.splice(0, __queue.length);
      for (const cb of due) cb(window.__vt);
    }
  };
  window.__jump = function (dtMs) { window.__vt += dtMs; };

  // A seeded PRNG in place of Math.random, installed for the same reason as the
  // clock: `orb.ts` scatters its 2000 particles with Math.random, so without
  // this two runs of this script produce two different clouds and the file
  // changes every time it is regenerated. The orb's own scattering formula is
  // untouched and draws from the stream exactly as it otherwise would; it just
  // gets the same stream twice. mulberry32, 32 bits of state.
  let __seed = 0x9e3779b9;
  Math.random = function () {
    __seed |= 0; __seed = (__seed + 0x6D2B79F5) | 0;
    let x = Math.imul(__seed ^ (__seed >>> 15), 1 | __seed);
    x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x;
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };

  // A stand-in for the AnalyserNode `voice.ts` hands the orb while JARVIS
  // speaks. Same shape (frequencyBinCount + getByteFrequencyData), same
  // fftSize-256 bin count, same 0.8 smoothing; the spectrum is computed from
  // the frame clock instead of arriving from Fish Audio. Every component is a
  // whole number of cycles per loop, so the envelope is exactly periodic.
  window.__speechAnalyser = function (opt) {
    const BINS = 128;                       // voice.ts: analyser.fftSize = 256
    const smoothed = new Float32Array(BINS);
    window.__bassMid = [];
    return {
      frequencyBinCount: BINS,
      getByteFrequencyData: function (out) {
        const t = window.__vt / 1000;
        const phase = ((t - opt.start) % opt.period + opt.period) % opt.period;
        const u = phase / opt.period;       // loop position, 0..1
        const tau = 2 * Math.PI * u;
        const smoothstep = function (a, b, x) {
          const s = Math.max(0, Math.min(1, (x - a) / (b - a)));
          return s * s * (3 - 2 * s);
        };
        // Phrases, with a real silence at the end of each one — the biggest
        // thing on screen, because the orb falls back and dims through it...
        const v = (u * opt.phrases) % 1;              // position inside a phrase
        const gate = smoothstep(0, 0.05, v) *
                     (1 - smoothstep(1 - opt.gap, 1 - opt.gap * 0.3, v));
        // ...syllables inside them, which never reach silence...
        const syl = Math.pow(0.5 - 0.5 * Math.cos(tau * opt.syllables), 1.6);
        // ...a faster flutter so it is not a metronome, and one slow swell.
        const flutter = 0.93 + 0.07 * Math.sin(tau * opt.flutter + 0.9);
        const swell = 0.94 + 0.06 * Math.sin(tau - 1.2);
        // base/depth are fitted: they put the smoothed bass and mid on top of
        // the measured trace. See the header.
        const amp = gate * (opt.base + opt.depth * syl) * flutter * swell;

        let bass = 0, mid = 0;
        for (let i = 0; i < BINS; i++) {
          const raw = Math.max(0, amp * (opt.peak - opt.tilt * i));
          smoothed[i] = opt.smoothing * smoothed[i] + (1 - opt.smoothing) * raw;
          const byte = Math.max(0, Math.min(255, Math.round(smoothed[i])));
          out[i] = byte;
          if (i < 8) bass += byte; else if (i < 24) mid += byte;
        }
        window.__bassMid.push([bass / (8 * 255), mid / (16 * 255)]);
      },
    };
  };
</script>
<canvas id="orb-canvas"></canvas>
<script type="module">
  import { createOrb } from './orb.js';
  window.__orb = createOrb(document.getElementById('orb-canvas'));
  window.__ready = true;
</script>
"""


def bundle_orb(out_dir: pathlib.Path) -> None:
    """Bundle `frontend/src/orb.ts` (and the three.js it imports) into one ES
    module the harness page can load. esbuild is already in
    `frontend/node_modules` as a Vite dependency; this adds nothing.

    The orb instance in the app is a module-local `const` inside `main.ts` with
    no global attached, and `main.ts` opens a WebSocket and a microphone on
    load — so it cannot be filmed and must not be edited to make it filmable.
    Loading the orb module by itself is the way in.
    """
    if not ESBUILD.exists():
        raise SystemExit(f"{ESBUILD} missing — run `npm install` in frontend/")
    subprocess.run(
        [str(ESBUILD), str(ORB_TS), "--bundle", "--format=esm",
         f"--outfile={out_dir / 'orb.js'}", "--log-level=warning"],
        check=True,
    )


def serve(root: pathlib.Path) -> tuple[socketserver.TCPServer, int]:
    """A loopback static server. ES-module imports are blocked from file://
    (opaque origin), so the harness has to be served over HTTP."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None  # type: ignore[method-assign]
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


async def render_frames(frames_dir: pathlib.Path, cfg: argparse.Namespace,
                        n_frames: int, start: float) -> list[pathlib.Path]:
    from playwright.async_api import async_playwright

    work = frames_dir / "harness"
    work.mkdir(parents=True, exist_ok=True)
    bundle_orb(work)
    (work / "orb-loop.html").write_text(HARNESS)
    httpd, port = serve(work)

    per_out = RENDER_HZ // cfg.fps          # orb frames per captured frame
    dt = 1000.0 / RENDER_HZ
    shots: list[pathlib.Path] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=[
                # Headless Chromium has no GPU here; without these the canvas
                # comes back black rather than failing loudly.
                "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            ])
            page = await browser.new_page(
                viewport={"width": cfg.width, "height": cfg.height},
                device_scale_factor=cfg.scale,
            )
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            await page.goto(f"http://127.0.0.1:{port}/orb-loop.html")
            # polling= matters: waitForFunction defaults to rAF, and rAF is ours.
            await page.wait_for_function("window.__ready === true", timeout=30000, polling=100)
            if errors:
                raise SystemExit(f"page errors: {errors}")

            await page.evaluate(
                """(opt) => {
                     window.__orb.setState('speaking');
                     window.__orb.setAnalyser(window.__speechAnalyser(opt));
                   }""",
                {"start": start, "period": cfg.seconds,
                 "phrases": round(PHRASE_HZ * cfg.seconds),
                 "syllables": round(SYLLABLE_HZ * cfg.seconds),
                 "flutter": round(FLUTTER_HZ * cfg.seconds),
                 "gap": PHRASE_GAP, "peak": SPEECH_PEAK, "tilt": SPEECH_TILT,
                 "base": SPEECH_BASE, "depth": SPEECH_DEPTH,
                 "smoothing": SPEECH_SMOOTHING},
            )

            # Warm up at t=0 until every lerp and the transition tumble have
            # converged, then jump the clock to just before START_TIME and let
            # the Z spring re-settle at the new phase. Nothing here is filmed.
            await page.evaluate("([n, dt]) => window.__advance(n, dt)", [WARMUP_FRAMES, dt])
            await page.evaluate(
                "([start, settle]) => window.__jump(1000 * (start - settle) - window.__vt)",
                [start, SETTLE_SECONDS],
            )
            await page.evaluate("([n, dt]) => window.__advance(n, dt)",
                                [int(SETTLE_SECONDS * RENDER_HZ), dt])
            await page.evaluate("() => { window.__bassMid = []; }")

            for i in range(n_frames):
                if i:
                    await page.evaluate("([n, dt]) => window.__advance(n, dt)", [per_out, dt])
                shot = frames_dir / f"f{i:05d}.png"
                await page.screenshot(path=str(shot))
                shots.append(shot)
                if i % 20 == 0:
                    print(f"  frame {i}/{n_frames}", flush=True)

            bm = await page.evaluate("() => window.__bassMid")
            # Compare in-phrase against in-phrase: the reference trace was taken
            # mid-phrase, so drop the quietest third of the loop, which is the
            # silences, before quoting a range.
            loud = sorted(bm, key=lambda r: r[0])[len(bm) // 3:]
            bass = [b for b, _ in loud]
            mid = [m for _, m in loud]
            floor = min(b for b, _ in bm)
            print(f"synthetic analyser, in phrase: "
                  f"bass {min(bass):.2f}-{max(bass):.2f} (mean {sum(bass)/len(bass):.2f}), "
                  f"mid {min(mid):.2f}-{max(mid):.2f} (mean {sum(mid)/len(mid):.2f}), "
                  f"quietest frame of the loop {floor:.2f}")
            print("   the real AnalyserNode measured, in phrase: "
                  "bass 0.63-0.87 (0.77), mid 0.35-0.64 (0.52), in the gap 0.00")
            if errors:
                raise SystemExit(f"page errors: {errors}")
            await browser.close()
    finally:
        httpd.shutdown()
    return shots


def crossfade(shots: list[pathlib.Path], n: int, k: int, seq: pathlib.Path) -> None:
    """Lay out the looping sequence: the first k frames are blended with the
    frames exactly one loop-period later, the rest are copied through."""
    seq.mkdir(parents=True, exist_ok=True)
    for j in range(k):
        alpha = j / k                       # 0 at the wrap, so frame 0 IS frame n
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-i", str(shots[j]), "-i", str(shots[n + j]),
             "-filter_complex",
             f"[0][1]blend=all_mode=normal:all_opacity={alpha:.6f}",
             "-frames:v", "1", str(seq / f"f{j:05d}.png")],
            check=True,
        )
    for j in range(k, n):
        shutil.copyfile(shots[j], seq / f"f{j:05d}.png")


def encode(seq: pathlib.Path, cfg: argparse.Namespace, gif: pathlib.Path) -> pathlib.Path:
    """GIF first (the README's hero), then an animated WebP of the same frames.

    The frames come off the browser supersampled (--scale), and the downscale
    happens here: averaging the 2x render is what turns single-pixel particle
    noise into smooth gradients. That is worth more than any encoder setting,
    both for how it looks and for the file size — every pixel of the orb
    changes every frame, so none of the diff-mode tricks that keep the
    dashboard walkthrough small apply here, and entropy is the whole cost.

    The palette is small on purpose: the orb is one blue ramp on near-black.
    What it cannot survive is error-diffusion dither, which invents fresh
    high-frequency noise on every frame; ordered bayer is stable frame to
    frame, and `dither=none` on a smooth downscaled gradient is smaller still.
    """
    gif.parent.mkdir(parents=True, exist_ok=True)
    scale = (f"scale={cfg.width}:{cfg.height}:flags=lanczos," if cfg.scale != 1 else "")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(cfg.fps),
         "-i", str(seq / "f%05d.png"),
         "-filter_complex",
         f"[0:v]{scale}split[a][b];"
         f"[a]palettegen=max_colors={GIF_COLORS}:stats_mode=full[p];"
         "[b][p]paletteuse=dither=none:new=0",
         "-loop", "0", str(gif)],
        check=True,
    )
    return encode_webp(seq, cfg, gif.with_suffix(".webp"))


def encode_webp(seq: pathlib.Path, cfg: argparse.Namespace, webp: pathlib.Path) -> pathlib.Path:
    """The same frames as an animated WebP — under half the GIF's bytes for a
    better picture, and GitHub renders it. This ffmpeg has no libwebp
    encoder built in, so `img2webp` (which ships with the same webp tools as
    `cwebp`) does it if it is there; if neither is, the GIF stands alone."""
    if not shutil.which("img2webp"):
        print("img2webp not on PATH — skipping the WebP", file=sys.stderr)
        return webp
    scaled = seq.parent / "webp-in"
    scaled.mkdir(exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(seq / "f%05d.png"),
         "-vf", f"scale={cfg.width}:{cfg.height}:flags=lanczos",
         str(scaled / "f%05d.png")],
        check=True,
    )
    subprocess.run(
        # -mixed is not a nicety: without it img2webp encodes every frame
        # LOSSLESSLY and the file comes out seven times the size of the GIF.
        ["img2webp", "-loop", "0", "-d", str(round(1000 / cfg.fps)), "-mixed",
         "-q", "72", "-m", "6",
         *sorted(str(p) for p in scaled.glob("f*.png")), "-o", str(webp)],
        check=True, capture_output=True,
    )
    return webp


def psnr(a: pathlib.Path, b: pathlib.Path) -> float:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b),
         "-filter_complex", "psnr", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    ).stderr
    m = re.search(r"average:([0-9.]+|inf)", out)
    if not m:
        raise SystemExit("could not read psnr from ffmpeg")
    return float("inf") if m.group(1) == "inf" else float(m.group(1))


def check_seam(seq: pathlib.Path, n: int) -> None:
    """The seam test. Compare the wrap — output frame n-1 followed by output
    frame 0 — against ordinary neighbouring frames. A wrap that is no worse
    than a normal frame step is a wrap nobody can find by eye."""
    wrap = psnr(seq / f"f{n - 1:05d}.png", seq / "f00000.png")
    steps = sorted(psnr(seq / f"f{i:05d}.png", seq / f"f{i + 1:05d}.png")
                   for i in range(0, n - 1, max(1, n // 24)))
    median = steps[len(steps) // 2]
    print(f"seam: wrap PSNR {wrap:.2f} dB vs neighbouring-frame median "
          f"{median:.2f} dB (worst {steps[0]:.2f}, best {steps[-1]:.2f})")
    if wrap < steps[0]:
        print("WARNING: the wrap is a bigger jump than any ordinary frame step —"
              " lengthen CROSSFADE_SECONDS.", file=sys.stderr)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=pathlib.Path, default=OUT_GIF)
    ap.add_argument("--width", type=int, default=WIDTH)
    ap.add_argument("--height", type=int, default=HEIGHT)
    ap.add_argument("--fps", type=int, default=FPS)
    ap.add_argument("--scale", type=int, default=SUPERSAMPLE,
                    help="render at this multiple of --width/--height and average down")
    ap.add_argument("--seconds", type=float, default=LOOP_SECONDS)
    ap.add_argument("--crossfade", type=float, default=CROSSFADE_SECONDS)
    ap.add_argument("--reference", action="store_true")
    ap.add_argument("--frames-only", action="store_true")
    ap.add_argument("--frames-dir", type=pathlib.Path, default=None)
    cfg = ap.parse_args()

    if cfg.reference:
        await take_reference()
        return
    if RENDER_HZ % cfg.fps:
        raise SystemExit(f"RENDER_HZ ({RENDER_HZ}) must be a whole multiple of --fps")
    for name, hz in (("syllable", SYLLABLE_HZ), ("phrase", PHRASE_HZ), ("flutter", FLUTTER_HZ)):
        if abs(hz * cfg.seconds - round(hz * cfg.seconds)) > 1e-9:
            raise SystemExit(f"{hz} Hz {name}s is not a whole number of cycles in "
                             f"{cfg.seconds}s — the envelope would not close the loop")
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg not on PATH")

    n = round(cfg.seconds * cfg.fps)        # frames in the finished loop
    k = round(cfg.crossfade * cfg.fps)      # frames of crossfade
    start, cost, naive = best_start(cfg.seconds)
    print(f"{cfg.seconds}s loop: {n} frames at {cfg.fps}fps, {k}-frame crossfade")
    print(f"start time {start:.2f}s — seam drift {cost:.1f}px, "
          f"against {naive:.1f}px starting at t=0")

    tmp = cfg.frames_dir or pathlib.Path(tempfile.mkdtemp(prefix="jarvis-orb-"))
    tmp.mkdir(parents=True, exist_ok=True)
    print(f"frames -> {tmp}")

    shots = await render_frames(tmp, cfg, n + k, start)
    if cfg.frames_only:
        return

    seq = tmp / "seq"
    crossfade(shots, n, k, seq)
    check_seam(seq, n)
    webp = encode(seq, cfg, cfg.out)
    for path in (cfg.out, webp):
        size = path.stat().st_size
        print(f"{path} — {size / 1024:.0f} KiB, {cfg.width}x{cfg.height}")
        if path == cfg.out and size > 3 * 1024 * 1024:
            print("WARNING: GIF over 3 MiB. Shrink it or drop a frame rate.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
