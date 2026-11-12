"""Draft a timeline by finding which slide is on the projector in the presenter video.

1. Sample the video once per second.
2. Locate the projected slide with SIFT features + a RANSAC homography against every PDF page.
   This copes with the camera being moved mid-talk and with the screen at an angle.
3. Near-identical pages (e.g. the same diagram with a different highlight) are told apart by
   warping the screen into slide space and comparing pixels.
4. Each change is refined to 0.1 s; short blips are dropped; long stretches without any slide
   (demos, Q&A) become `presenter`.
5. Context rules decide where the speaker is shown large (apply_context).

With a screen recording, its slide changes are matched to the camera's to sync the two, and what
isn't a slide there (demos) becomes `screen`. The result is a draft for a human to review, with a
confidence note on every line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from . import cache, ffmpeg
from .timeline import Event

SIZE = (960, 540)  # analysis resolution for frames and slides
CMP = (240, 135)  # resolution for pixel comparison in slide space
MIN_INLIERS = 12
MIN_SCORE = 0.2  # correlation between the rectified screen and the slide
HYSTERESIS = 0.03  # another page must beat the current one by this much to switch
MIN_MARGIN = 0.015  # over the runner-up page; below this the screen position is re-checked
MIN_HOLD = 2  # samples a slide must be seen to count (drops flicker / walk-bys)
NO_SLIDE_PRESENTER = 15.0  # seconds without a recognisable slide before switching to presenter
SCREEN_SLIDE_MIN = 0.8  # a screen recording shows slides pixel-perfect; weaker matches are something else
SCREEN_MIN = 5.0  # seconds of non-slide content on the screen recording before showing it as `screen`
LOW_MARGIN = 0.03  # runner-up page almost as good: worth a look


@dataclass
class SlideRef:
    number: int  # 1-based
    gray: np.ndarray  # SIZE
    small: np.ndarray  # CMP, blurred, float
    keypoints: list
    descriptors: np.ndarray | None


@dataclass
class Sample:
    time: float
    slide: int | None
    score: float
    margin: float
    H: np.ndarray | None


DUPLICATE = 0.99  # page-to-page similarity above which two pages count as the same
SHIFT = 8  # pixels (at CMP size) of misregistration tolerated when comparing


def _small(img: np.ndarray) -> np.ndarray:
    """Structure-only thumbnail for comparison: band-pass (difference of Gaussians) removes the
    projector's uneven brightness and washed-out contrast."""
    x = cv2.resize(img, CMP, interpolation=cv2.INTER_AREA).astype(np.float32)
    return cv2.GaussianBlur(x, (0, 0), 1.5) - cv2.GaussianBlur(x, (0, 0), 8.0)


GRID = (4, 3)  # cells for the robust comparison
MIN_CELL_STD = 1.0  # cells with less structure than this in the slide carry no information


def _zncc(screen: np.ndarray, slide: np.ndarray) -> float:
    """Robust similarity of the rectified screen and a slide: median over grid cells.

    Each cell of the slide is matched against the same area of the screen, allowing ±SHIFT px of
    misregistration. Only cells where the slide has content count, and the median ignores the cells
    spoiled by the speaker standing in front, the screen edge, or parts projected off the screen.
    """
    h, w = slide.shape
    gx, gy = GRID
    scores = []
    for j in range(gy):
        for i in range(gx):
            x0, x1 = i * w // gx, (i + 1) * w // gx
            y0, y1 = j * h // gy, (j + 1) * h // gy
            template = slide[y0:y1, x0:x1]
            if template.std() < MIN_CELL_STD:
                continue
            region = screen[max(0, y0 - SHIFT) : y1 + SHIFT, max(0, x0 - SHIFT) : x1 + SHIFT]
            scores.append(float(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED).max()))
    return float(np.median(scores)) if scores else 0.0


class Matcher:
    """Finds the screen with SIFT features, then identifies the slide by pixel correlation.

    Features are only needed to (re)locate the screen: once the homography is known, comparing the
    rectified screen with all pages is cheap and also works for pages with almost no features
    (a single word) and for near-identical pages.
    """

    def __init__(self, slide_pngs: list[Path], fixed: bool = False):
        self.fixed = fixed  # the screen can't move (screen recordings): never re-locate it once found
        self.sift = cv2.SIFT_create(nfeatures=1500)
        self.clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        self.bf = cv2.BFMatcher(cv2.NORM_L2)
        self.slides: list[SlideRef] = []
        for i, p in enumerate(slide_pngs, 1):
            g = cv2.resize(cv2.imread(str(p), cv2.IMREAD_GRAYSCALE), SIZE, interpolation=cv2.INTER_AREA)
            kp, des = self.sift.detectAndCompute(g, None)
            self.slides.append(SlideRef(i, g, _small(g), kp, des))
        self.H: np.ndarray | None = None  # slide space → frame, from the last confident localization
        self.recent = 0.0  # running match score of the current slide; a drop suggests the camera moved
        # Pages that look the same (duplicates, builds that change little) can't be told apart on
        # the projector; margins are measured against the best page that is *not* a near-duplicate.
        n = len(self.slides)
        self.same = np.eye(n, dtype=bool)
        for a in range(n):
            for b in range(a + 1, n):
                pa, pb = self.slides[a].small.ravel(), self.slides[b].small.ravel()
                if np.corrcoef(pa, pb)[0, 1] > DUPLICATE:  # whole-page comparison, not the robust one
                    self.same[a, b] = self.same[b, a] = True

    def warp(self, frame: np.ndarray, H: np.ndarray) -> np.ndarray:
        """Projected screen, rectified into slide space."""
        w = cv2.warpPerspective(frame, H, SIZE, flags=cv2.WARP_INVERSE_MAP | cv2.INTER_LINEAR)
        return _small(w)

    def by_pixels(self, frame: np.ndarray, H: np.ndarray, current: int | None = None) -> tuple[int, float, float]:
        """(best slide, its score, margin over the runner-up) for the screen under H.

        Hysteresis: the current slide is kept unless another page is clearly better, so a page that
        differs only by a small highlight doesn't flicker while the speaker blocks that spot."""
        warped = self.warp(frame, H)
        scores = np.array([_zncc(warped, s.small) for s in self.slides])
        best = int(np.argmax(scores))
        if current is not None and scores[current - 1] >= scores[best] - HYSTERESIS:
            best = current - 1
        others = scores[~self.same[best]]
        margin = float(scores[best] - others.max()) if len(others) else 1.0
        return best + 1, float(scores[best]), margin

    def _sane(self, H: np.ndarray) -> bool:
        w, h = SIZE
        corners = cv2.perspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2), H)
        quad = corners.reshape(-1, 2).astype(np.float32)
        area = cv2.contourArea(quad)
        return cv2.isContourConvex(quad) and 0.02 * w * h < area < 1.5 * w * h

    def localize(self, frame: np.ndarray) -> list[tuple[np.ndarray, SlideRef]]:
        """Candidate (homography, page it came from), strongest feature match first."""
        kp, des = self.sift.detectAndCompute(self.clahe.apply(frame), None)
        if des is None or len(kp) < MIN_INLIERS:
            return []
        candidates = []
        for s in self.slides:
            if s.descriptors is None or len(s.keypoints) < 4:
                continue
            pairs = self.bf.knnMatch(s.descriptors, des, k=2)
            good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
            if len(good) < MIN_INLIERS:
                continue
            src = np.float32([s.keypoints[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst = np.float32([kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
            if H is not None and mask.sum() >= MIN_INLIERS and self._sane(H):
                candidates.append((int(mask.sum()), H, s))
        candidates.sort(key=lambda c: -c[0])
        return [(H, s) for _, H, s in candidates[:4]]

    def relocalize(self, frame: np.ndarray) -> np.ndarray | None:
        """New screen position, trusted only if the page whose features produced it also matches it
        in pixels (a wrong position paired with a wrong page can otherwise look plausible)."""
        best = None
        for H, page in self.localize(frame):
            own = _zncc(self.warp(frame, H), page.small)
            if own >= MIN_SCORE and (best is None or own > best[0]):
                best = (own, H)
        return best[1] if best else None

    def classify(self, frame: np.ndarray, current: int | None = None) -> tuple[int | None, float, float]:
        """(slide or None, score, margin)."""
        result = self.by_pixels(frame, self.H, current) if self.H is not None else None
        stable = (
            result is not None
            and result[0] == current
            and result[1] >= max(MIN_SCORE, 0.8 * self.recent)
        )
        if not stable and not (self.fixed and self.H is not None):
            # New slide, weaker match or no position yet: check whether the camera moved.
            H = self.relocalize(frame)
            if H is not None:
                moved = self.by_pixels(frame, H, current)
                if result is None or moved[1] > result[1]:
                    self.H, result = H, moved
        if result is None or result[1] < MIN_SCORE or (result[2] < MIN_MARGIN and result[0] != current):
            return None, 0.0, 0.0  # keep the old H: occlusions are usually brief
        self.recent = result[1] if result[0] != current else 0.7 * self.recent + 0.3 * result[1]
        return result


def frames(video: Path, fps: float, start: float = 0.0, duration: float | None = None):
    """Yield (time, gray frame at SIZE) from ffmpeg without loading the whole video."""
    w, h = SIZE
    args = ["-ss", f"{start:.3f}", "-i", str(video)]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    args += ["-vf", f"fps={fps},scale={w}:{h},format=gray", "-f", "rawvideo", "-"]
    for n, buf in enumerate(ffmpeg.stream(args, w * h, label=f"reading {video.name}")):
        yield start + n / fps, np.frombuffer(buf, np.uint8).reshape(h, w)


def refine(matcher: Matcher, H: np.ndarray, video: Path, t0: float, t1: float, old: int, new: int) -> float:
    """Find the change between samples t0 and t1 at 10 fps: first frame that looks more like `new`."""
    old_s, new_s = matcher.slides[old - 1].small, matcher.slides[new - 1].small
    for t, frame in frames(video, 10, t0, t1 - t0 + 0.05):
        warped = matcher.warp(frame, H)
        if _zncc(warped, new_s) > _zncc(warped, old_s):
            return round(t, 1)
    return t1


@dataclass
class Change:
    time: float
    kind: str  # "slide", "presenter" or "screen"
    slide: int | None = None
    score: float = 0.0
    margin: float = 0.0
    style: str = ""  # "" (content large), "speaker" (presenter large), ...
    note: str = ""


def smooth(samples: list[Sample]) -> list[tuple[int, int, int | None]]:
    """Runs (first index, end index, slide) after dropping runs shorter than MIN_HOLD."""
    runs: list[list] = []
    for i, s in enumerate(samples):
        if runs and runs[-1][2] == s.slide:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1, s.slide])
    out: list[list] = []
    for r in runs:
        if r[1] - r[0] < MIN_HOLD and out:
            out[-1][1] = r[1]  # absorb blips into the previous run
        elif out and out[-1][2] == r[2]:
            out[-1][1] = r[1]
        else:
            out.append(r)
    return [tuple(r) for r in out]


def classify(video: Path, matcher: Matcher, fps: float = 1.0, progress=None) -> list[Sample]:
    """Per-sample slide labels (None: no PDF page recognised)."""
    samples: list[Sample] = []
    current: int | None = None
    for t, frame in frames(video, fps):
        slide, score, margin = matcher.classify(frame, current)
        if slide is not None:
            current = slide
        samples.append(Sample(t, slide, score, margin, matcher.H))
        if progress:
            progress(t)
    return samples


CLASSIFY_VERSION = 1  # bump when classification changes, to invalidate cached samples


def classify_cached(
    video: Path, slide_pngs: list[Path], cache_dir: Path | None, fixed: bool = False, fps: float = 1.0, progress=None
) -> tuple[list[Sample], Matcher]:
    """classify(), with the samples cached in cache_dir: re-drafting with other rules is then instant."""
    matcher = Matcher(slide_pngs, fixed=fixed)
    if cache_dir is None:
        return classify(video, matcher, fps, progress), matcher
    pages = [f"{p.parent.name}/{p.name}" for p in slide_pngs]
    path = cache_dir / f"classify-{cache.key('classify', CLASSIFY_VERSION, cache.file_fingerprint(video), pages, fixed, fps)}.json"
    if path.exists():
        data = json.loads(path.read_text())
        samples = [Sample(d["t"], d["slide"], d["score"], d["margin"], np.array(d["H"]) if d["H"] else None) for d in data]
        return samples, matcher
    samples = classify(video, matcher, fps, progress)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([
        {"t": s.time, "slide": s.slide, "score": s.score, "margin": s.margin, "H": s.H.tolist() if s.H is not None else None}
        for s in samples
    ]))  # fmt: skip
    return samples, matcher


def build_changes(
    samples: list[Sample], matcher: Matcher, video: Path, fps: float = 1.0, other: str = "presenter", min_other: float = NO_SLIDE_PRESENTER
) -> list[Change]:
    """Turn samples into timeline changes.

    Stretches without a recognised slide become `presenter` before the first slide (nothing relevant
    on screen yet) and `other` after it, if they last at least `min_other` seconds; shorter gaps (the
    speaker walking in front of the screen) keep the previous slide.
    """
    changes: list[Change] = []
    last: tuple[str, int | None] | None = None
    seen_slide = False
    for i, end, slide in smooth(samples):
        t = samples[i].time
        if slide is None:
            if not seen_slide:
                kind = "presenter"
            elif (end - i) / fps >= min_other:
                kind = other
            else:
                continue
            if last != (kind, None):
                changes.append(Change(t, kind))
                last = (kind, None)
            continue
        seen_slide = True
        if last is not None and last[0] == "slide" and (slide == last[1] or matcher.same[slide - 1, last[1] - 1]):
            continue  # same page, or an identical duplicate of it
        seg = [s for s in samples[i:end] if s.slide == slide]
        H = samples[i].H
        if last is not None and last[0] == "slide" and i > 0 and H is not None:
            t = refine(matcher, H, video, samples[i - 1].time, samples[i].time, last[1], slide)
        changes.append(
            Change(t, "slide", slide, float(np.median([s.score for s in seg])), float(np.median([s.margin for s in seg])))
        )
        last = ("slide", slide)
    return changes


def draft(video: Path, slide_pngs: list[Path], fps: float = 1.0, progress=None, cache_dir: Path | None = None) -> list[Change]:
    """Draft from the presenter video alone (slides recognised on the projector)."""
    samples, matcher = classify_cached(
        video, slide_pngs, cache_dir, fps=fps, progress=progress and (lambda t: progress("camera", t))
    )
    return build_changes(samples, matcher, video, fps)


def estimate_offset(camera: list[Change], screen: list[Change]) -> tuple[float, int]:
    """Presenter time minus screen time, from slide changes seen in both recordings.

    Every pair of changes with the same (previous slide → slide) votes for an offset; the largest
    cluster of votes wins, which ignores the pairs that only match by coincidence.
    """

    def transitions(changes: list[Change]) -> list[tuple[float, int, int]]:
        out, prev = [], None
        for c in changes:
            if c.kind == "slide":
                if prev is not None:
                    out.append((c.time, prev, c.slide))
                prev = c.slide
            else:
                prev = None
        return out

    cam, scr = transitions(camera), transitions(screen)
    votes = np.array([tc - ts for tc, a, b in cam for ts, c, d in scr if (a, b) == (c, d)])
    if len(votes) == 0:
        return 0.0, 0
    best = max(votes, key=lambda v: np.sum(np.abs(votes - v) < 1.5))
    close = votes[np.abs(votes - best) < 1.5]
    return round(float(np.median(close)), 2), len(close)


@dataclass
class ScreenDraft:
    changes: list[Change]  # in presenter time
    offset: float
    matches: int  # slide changes that agreed on the offset

    @property
    def note(self) -> str:
        return f"auto: {self.matches} slide changes agree" if self.matches else "could not sync automatically; set by hand"


def draft_with_screen(
    video: Path, screen: Path, slide_pngs: list[Path], fps: float = 1.0, progress=None, cache_dir: Path | None = None
) -> ScreenDraft:
    """Draft from the screen recording (clean slides; anything else on screen is a demo), synced to the
    presenter video by matching the slide changes both of them show."""
    cam_samples, cam_matcher = classify_cached(
        video, slide_pngs, cache_dir, fps=fps, progress=progress and (lambda t: progress("camera", t))
    )
    cam = build_changes(cam_samples, cam_matcher, video, fps)
    # The screen never moves: locate the slide area once, then compare pixels only.
    scr_samples, scr_matcher = classify_cached(
        screen, slide_pngs, cache_dir, fixed=True, fps=fps, progress=progress and (lambda t: progress("screen", t))
    )
    # A slide on the screen recording matches near-perfectly. Anything weaker is code, a browser, someone
    # else's slides, or a slide mid-animation: show the recording itself there.
    scr_samples = [s if s.slide is None or s.score >= SCREEN_SLIDE_MIN else replace(s, slide=None) for s in scr_samples]
    scr = build_changes(scr_samples, scr_matcher, screen, fps, other="screen", min_other=SCREEN_MIN)
    offset, n = estimate_offset(cam, scr)
    shifted = [Change(round(c.time + offset, 1), c.kind, c.slide, c.score, c.margin) for c in scr]
    # Drop what happened on screen before the camera started; the last of it holds at time 0.
    changes = [c for i, c in enumerate(shifted) if i + 1 == len(shifted) or shifted[i + 1].time > 0]
    if changes:
        changes[0].time = max(0.0, changes[0].time)
    return ScreenDraft(changes, offset, n)


# Context rules: when the presenter should be large (see apply_context).
LOW_TEXT_WORDS = 10  # slides with at most this many words (titles, one-word questions) show the speaker large
RHYTHM_AFTER = 90.0  # slides held longer than this alternate between slide and speaker
RHYTHM_SLIDE, RHYTHM_SPEAKER = 45.0, 30.0  # seconds of each in that rhythm
RHYTHM_MARGIN = 15.0  # no switch this close to the next change


IMAGE_HEAVY = 0.2  # pages whose images cover this share of the page are something to look at


def page_words(pdf: Path) -> list[int]:
    """Words per page; pages that are mostly picture (screenshots, diagrams as images) count as full."""
    from .slides import page_stats

    return [10_000 if picture >= IMAGE_HEAVY else words for words, picture in page_stats(pdf)]


def apply_context(changes: list[Change], words: list[int], duration: float) -> list[Change]:
    """Decide where the presenter is shown large (the `speaker` style):

    1. before the talk properly starts (the first two slide changes in a row), non-slide content
       on screen (organiser intro, title animations) is shown small next to a large speaker;
    2. slides with little text (titles, one-word questions) show the speaker large;
    3. slides held for a long time alternate: the slide large, then the speaker large for a while;
    4. demos (screen recording after the start) stay large.
    """
    talk_start = next(
        (a.time for a, b in zip(changes, changes[1:]) if a.kind == "slide" and b.kind == "slide"),
        next((c.time for c in changes if c.kind == "slide"), duration),
    )
    out: list[Change] = []
    for i, c in enumerate(changes):
        end = changes[i + 1].time if i + 1 < len(changes) else duration
        c = replace(c)
        if c.kind == "screen" and c.time < talk_start:
            c.style, c.note = "speaker", "intro: speaker large"
        elif c.kind == "slide" and c.slide and words[c.slide - 1] <= LOW_TEXT_WORDS:
            c.style, c.note = "speaker", f"little text on the slide: speaker large; {c.note}".rstrip("; ")
        out.append(c)
        if c.kind == "slide" and not c.style and end - c.time > RHYTHM_AFTER:
            t, speaker = c.time + RHYTHM_SLIDE, True
            while t < end - RHYTHM_MARGIN:
                out.append(Change(round(t, 1), "slide", c.slide, style="speaker" if speaker else "",
                                  note="rhythm: speaker large" if speaker else "rhythm: slide large"))  # fmt: skip
                t += RHYTHM_SPEAKER if speaker else RHYTHM_SLIDE
                speaker = not speaker
    return out


def draft_talk(
    master: Path, screen: Path | None, pdf: Path, slide_pngs: list[Path], duration: float, cache_dir: Path | None, progress=None
) -> tuple[list[Change], ScreenDraft | None]:
    """The draft of a whole talk (converted videos), context rules applied; also the screen sync if any.

    progress(label, t): seconds of the "camera" or "screen" video recognised so far.
    """
    if screen is not None:
        synced = draft_with_screen(master, screen, slide_pngs, progress=progress, cache_dir=cache_dir)
        changes = synced.changes
    else:
        synced, changes = None, draft(master, slide_pngs, progress=progress, cache_dir=cache_dir)
    return apply_context(changes, page_words(pdf), duration), synced


def to_events(changes: list[Change]) -> list[Event]:
    """Timeline commands, each with a note on how sure the match was (CHECK: worth a look)."""
    events = []
    for i, c in enumerate(changes):
        if c.kind == "slide":
            if c.note.startswith("rhythm"):
                note = c.note
            else:
                note = f"match {c.score:.2f}, margin {c.margin:.2f}" + ("  CHECK" if c.margin < LOW_MARGIN else "")
                if c.note:
                    note += f"; {c.note.split(';')[0]}"
            events.append(Event(c.time, "slide", c.slide, c.style, note=note))
        elif c.kind == "screen":
            events.append(Event(c.time, "screen", style=c.style, note=c.note or "not a slide: demo or other content"))
        else:
            first = not any(x.kind == "slide" for x in changes[:i])
            events.append(Event(c.time, "presenter", note="no slide yet" if first else "no slide visible"))
    return events

