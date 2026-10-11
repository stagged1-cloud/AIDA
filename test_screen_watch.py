"""Tests for screen_watch.py (live assistant M2). Run on its own: .venv\\Scripts\\python.exe test_screen_watch.py

Synthetic numpy frames throughout; the one real-capture test is skipped if mss cannot grab. Nothing in
here writes pixels to disk. Run measure_screen_watch.py for real CPU/RAM numbers.
"""
import os
import time

import numpy as np
import tempfile
import unittest
from pathlib import Path

import screen_watch as sw


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def frame(value=0, h=360, w=640):
    """Plain BGRA frame of one grey level."""
    return np.full((h, w, 4), value, dtype=np.uint8)


class FakeBackend:
    """Stands in for mss: per-monitor frames set by the test."""

    def __init__(self, frames):
        self.frames = frames
        self.grabs = 0

    def monitors(self):
        return sorted(self.frames)

    def grab(self, mon):
        self.grabs += 1
        return self.frames[mon]

    def close(self):
        pass


R1, R2 = (0, 0, 640, 360), (640, 0, 640, 360)


class HotplugBackend:
    """Fake mss with a live 'real' layout and a stale cached copy refreshed only by refresh(), like mss."""

    def __init__(self, rects):
        self.real = dict(rects)
        self.cached = dict(rects)
        self.frames = {m: frame(0, r[3], r[2]) for m, r in rects.items()}
        self.grabbed, self.fail, self.refreshes, self.sig_fixed = [], set(), 0, False

    def plug(self, mon, rect):
        self.real[mon] = rect
        self.frames[mon] = frame(0, rect[3], rect[2])

    def unplug(self, mon):
        self.real.pop(mon, None)

    def signature(self):
        return "fixed" if self.sig_fixed else tuple(sorted(self.real.items()))

    def refresh(self):
        self.refreshes += 1
        self.cached = dict(self.real)

    def layout(self):
        return dict(self.cached)

    def monitors(self):
        return sorted(self.cached)

    def grab(self, mon):
        if mon in self.fail:
            raise OSError("gdi grab failed")
        self.grabbed.append(mon)
        return self.frames[mon]

    def close(self):
        pass


ON = {"screen_watch_enabled": True, "screen_watch_pause_on_fullscreen": False}


def make_watch(frames, cfg=None, clock=None, **kw):
    be = FakeBackend(frames)
    w = sw.ScreenWatch({**ON, **(cfg or {})}, backend_factory=lambda: be, clock=clock or FakeClock(), **kw)
    return w, be


# ------------------------------------------------------------------ ring buffer
class ScreenWatchTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def test_ring_evicts_oldest_per_monitor_by_count(self):
        # T1: with a 3-frame cap, the 4th push on the same monitor drops the first
        r = sw.FrameRing(max_frames=3, max_bytes=10**6, max_age_s=999, clock=FakeClock())
        for i in range(4):
            r.push(1, bytes([i]) * 10)
        assert len(r) == 3
        assert r.latest(1)["jpeg"] == bytes([3]) * 10


    def test_ring_count_cap_is_per_monitor(self):
        # T2: monitor 2's frames are not evicted by monitor 1 filling up
        r = sw.FrameRing(max_frames=2, max_bytes=10**6, max_age_s=999, clock=FakeClock())
        r.push(2, b"b" * 5)
        for _ in range(3):
            r.push(1, b"a" * 5)
        assert r.latest(2) is not None and len(r) == 3


    def test_ring_byte_cap_drops_oldest_first(self):
        # T3: 100-byte cap, three 40-byte frames -> oldest goes
        r = sw.FrameRing(max_frames=10, max_bytes=100, max_age_s=999, clock=FakeClock())
        r.push(1, b"x" * 40)
        r.push(2, b"y" * 40)
        r.push(1, b"z" * 40)
        assert r.nbytes() <= 100 and len(r) == 2
        assert r.latest(1)["jpeg"] == b"z" * 40


    def test_ring_age_cap(self):
        # T4: frames older than max_age_s disappear on read
        c = FakeClock()
        r = sw.FrameRing(max_frames=10, max_bytes=10**6, max_age_s=30, clock=c)
        r.push(1, b"old")
        c.advance(31)
        assert r.latest(1) is None


    def test_ring_clear_zeroes_buffers(self):
        # T5: clear() overwrites the stored bytes before dropping them (best-effort wipe)
        r = sw.FrameRing(clock=FakeClock())
        r.push(1, b"secret")
        buf = r._items[0][2]
        r.clear()
        assert len(r) == 0 and bytes(buf) == b"\0" * 6


    # ------------------------------------------------------------------ change detection
    def test_identical_frames_no_change(self):
        # T6: first sample is the baseline event, identical frames after that give nothing
        d = sw.ChangeDetector(settle=0)
        assert d.feed(sw.sample(frame(50)), 0)["fraction"] == 1.0
        assert all(d.feed(sw.sample(frame(50)), t) is None for t in range(1, 5))


    def test_small_noise_below_level_ignored(self):
        # T7: +/-5 grey levels of noise everywhere is under the level-10 cut-off
        d = sw.ChangeDetector(settle=0)
        d.feed(sw.sample(frame(100)), 0)
        rng = np.random.default_rng(1)
        noisy = frame(100)
        noisy[..., 1] = (100 + rng.integers(-5, 6, noisy.shape[:2])).astype(np.uint8)
        assert d.feed(sw.sample(noisy), 1) is None


    def test_tiny_area_below_threshold_ignored(self):
        # T8: a big change in a tiny patch (under 0.5% of cells) does not count
        d = sw.ChangeDetector(threshold=0.005, settle=0)
        d.feed(sw.sample(frame(0)), 0)
        f = frame(0)
        f[:8, :32] = 255                               # 1 x 4 cells of 45 x 80 = 0.1%
        assert d.feed(sw.sample(f), 1) is None


    def test_big_change_detected(self):
        # T9: whole screen changes -> fraction 1.0
        d = sw.ChangeDetector(settle=0)
        d.feed(sw.sample(frame(0)), 0)
        ev = d.feed(sw.sample(frame(200)), 1)
        assert ev is not None and ev["fraction"] == 1.0 and ev["ts"] == 1


    def test_partial_change_fraction(self):
        # T10: left quarter changes -> fraction about 0.25
        d = sw.ChangeDetector(settle=0)
        d.feed(sw.sample(frame(0)), 0)
        f = frame(0)
        f[:, :160] = 200
        ev = d.feed(sw.sample(f), 1)
        assert ev is not None and abs(ev["fraction"] - 0.25) < 0.01


    def test_debounce_waits_for_settle(self):
        # T11: settle=2 -> a moving screen fires nothing until 2 still samples, then exactly one event
        d = sw.ChangeDetector(settle=2)
        d.feed(sw.sample(frame(0)), 0)
        assert d.feed(sw.sample(frame(100)), 1) is None     # moving
        assert d.feed(sw.sample(frame(150)), 2) is None     # still moving
        assert d.feed(sw.sample(frame(150)), 3) is None     # still 1
        ev = d.feed(sw.sample(frame(150)), 4)               # still 2 -> event
        assert ev is not None and ev["ts"] == 4
        assert d.feed(sw.sample(frame(150)), 5) is None     # new baseline, quiet again


    def test_never_settling_screen_still_keeps_frames(self):
        # A game never stops moving: after moving_keep_s a 'moving' frame is kept anyway (11 Oct silent-commentator bug)
        d = sw.ChangeDetector(settle=2, moving_keep_s=4)
        d.feed(sw.sample(frame(0)), 0)
        evs = [d.feed(sw.sample(frame(50 + 40 * (i % 2))), i) for i in range(1, 12)]
        kept = [e for e in evs if e]
        assert kept and all(e.get("moving") for e in kept)
        old = sw.ChangeDetector(settle=2)                  # moving_keep_s 0: old behaviour, nothing ever kept
        old.feed(sw.sample(frame(0)), 0)
        assert not any(old.feed(sw.sample(frame(50 + 40 * (i % 2))), i) for i in range(1, 12))

    def test_moving_keep_goes_to_ring_not_changes(self):
        clock = [0.0]
        w, be = make_watch({1: frame(0)}, {"screen_watch_settle": 2, "screen_watch_moving_keep_s": 2,
                                           "screen_watch_monitors": [1]}, clock=lambda: clock[0])
        w.tick()
        n = len(w.recent_changes())
        for i in range(1, 6):
            clock[0] = float(i)
            be.frames[1] = frame(60 + 60 * (i % 2))
            w.tick()
        assert len(w.recent_changes()) == n and w.counters.get("moving_keeps", 0) >= 1
        assert w.get_latest_frame(1)["ts"] > 0

    def test_shape_change_counts_as_full_change(self):
        # T12: resolution change (monitor re-plugged) is a full change, not a crash
        assert sw.change_fraction(np.zeros((2, 2)), np.zeros((3, 3))) == 1.0


    # ------------------------------------------------------------------ watcher behaviour
    def test_multi_monitor_independent(self):
        # T13: change on monitor 2 only fires an event for monitor 2
        w, be = make_watch({1: frame(0), 2: frame(0)}, {"screen_watch_settle": 0})
        w.tick()
        assert {c["monitor"] for c in w.recent_changes()} == {1, 2}
        be.frames[2] = frame(200)
        w.tick()
        assert [c["monitor"] for c in w.recent_changes()][-1] == 2 and len(w.recent_changes()) == 3
        assert w.get_latest_frame(1)["jpeg"][:2] == b"\xff\xd8"      # JPEG in RAM


    def test_monitor_list_filter(self):
        # T14: screen_watch_monitors [2] grabs only monitor 2
        w, be = make_watch({1: frame(0), 2: frame(0)}, {"screen_watch_monitors": [2]})
        w.tick()
        assert be.grabs == 1 and w.get_latest_frame(1) is None


    def test_pause_resume_wipes_and_skips(self):
        # T15: pause wipes the ring and stops grabbing; resume grabs again
        w, be = make_watch({1: frame(0)})
        w.tick()
        assert len(w.ring) == 1
        w.pause("hotkey")
        assert len(w.ring) == 0
        n = be.grabs
        w.tick()
        assert be.grabs == n
        w.resume()
        w.tick()
        assert be.grabs == n + 1


    def test_auto_pause_from_mute_and_game(self):
        # T16: paused_fn (Mute) and guard_fn (game/fullscreen) both stop grabs and wipe the ring
        state = {"mute": "", "game": False}
        w, be = make_watch({1: frame(0)}, paused_fn=lambda: state["mute"], guard_fn=lambda: state["game"])
        w.tick()
        state["mute"] = "muted"
        w.tick()
        assert be.grabs == 1 and len(w.ring) == 0 and w.status()["skipped"] == 1
        state["mute"], state["game"] = "", True
        w.tick()
        assert be.grabs == 1


    def test_stop_idempotent_and_thread_runs(self):
        # T17: real thread with fake backend; stop twice is fine; ring wiped on stop
        w, be = make_watch({1: frame(0)}, clock=time.monotonic, base=self.tmp)
        assert w.start()
        for _ in range(100):
            if be.grabs:
                break
            time.sleep(0.02)
        assert w.watching and be.grabs >= 1
        w.stop()
        w.stop()
        assert not w.running and len(w.ring) == 0 and w.status()["state"] == "off"


    def test_no_files_written(self):
        # T18: a full run of ticks leaves cwd and the temp dir untouched
        old = os.getcwd(); os.chdir(self.tmp); self.addCleanup(os.chdir, old)
        before = set(os.listdir(self.tmp))
        here = set(os.listdir(sw.BASE))
        w, be = make_watch({1: frame(0), 2: frame(9)}, {"screen_watch_settle": 0}, base=self.tmp)
        for v in (0, 100, 200):
            be.frames[1] = frame(v)
            w.tick()
        w.stop()
        assert set(os.listdir(self.tmp)) == before and set(os.listdir(sw.BASE)) == here


    # ------------------------------------------------------------------ config gates
    def test_disabled_by_default(self):
        # T19: empty config -> not allowed, start() refuses without making a thread
        assert sw.DEFAULTS["screen_watch_enabled"] is False
        assert sw.watch_allowed({}) == (False, "disabled")
        w = sw.ScreenWatch({}, base=self.tmp)
        assert w.start() is False and not w.running


    def test_shipped_config_is_off(self):
        # T20: config.yaml has an explicit on/off switch (the user turned it on 10 Oct 23:31; code default stays off, T19)
        import yaml
        cfg = yaml.safe_load((sw.BASE / "config.example.yaml").read_text(encoding="utf-8"))
        assert isinstance(cfg.get("screen_watch_enabled"), bool)


    def test_enabled_allows_on_any_machine(self):
        # T21: no per-machine gate (the user dropped it); enabled is enough, and no gate key exists
        assert sw.watch_allowed({"screen_watch_enabled": True}) == (True, "ok")
        assert not any("work" in k for k in sw.DEFAULTS)


    # ------------------------------------------------------------------ adaptive rate
    def test_adaptive_rate_with_fake_clock(self):
        # T23: idle -> 5 s; after a change -> 1 s for the hold period; then back to 5 s
        c = FakeClock()
        r = sw.RateController(active_s=1, idle_s=5, hold_s=10, clock=c)
        assert r.interval() == 5
        r.note_change()
        c.advance(9)
        assert r.interval() == 1
        c.advance(2)
        assert r.interval() == 5


    # ------------------------------------------------------------------ monitor hot-plug
    def _hotplug(self, rects, cfg=None):
        be = HotplugBackend(rects)
        logs = []
        w = sw.ScreenWatch({**ON, "screen_watch_settle": 0, **(cfg or {})}, backend_factory=lambda: be,
                           clock=FakeClock(), log=lambda who, t: logs.append(t), base=self.tmp)
        return w, be, logs

    def test_hotplug_unplug_two_to_one(self):
        # T25: unplug monitor 2 -> next pass re-enumerates, stops grabbing 2, logs "2 -> 1"
        w, be, logs = self._hotplug({1: R1, 2: R2})
        w.tick()
        be.unplug(2)
        w.tick()
        assert w.monitor_rects() == {1: R1} and w.monitor_changes == 1
        assert be.grabbed[-1] == 1 and 2 not in w.detectors
        assert any("monitors changed 2 -> 1" in t for t in logs)

    def test_hotplug_replug_one_to_two(self):
        # T26: plug a monitor in -> it is picked up on the next pass and gets a baseline frame
        w, be, logs = self._hotplug({1: R1})
        w.tick()
        be.plug(2, R2)
        w.tick()
        assert set(w.monitor_rects()) == {1, 2} and w.get_latest_frame(2) is not None
        assert any("monitors changed 1 -> 2" in t for t in logs)

    def test_hotplug_rect_change_resets_monitor(self):
        # T27: resolution change on monitor 1 wipes its frames/detector, monitor 2 untouched
        w, be, _ = self._hotplug({1: R1, 2: R2})
        w.tick()
        det2 = w.detectors[2]
        be.plug(1, (0, 0, 1280, 720))
        be.frames[1] = frame(0, 180, 320)
        w.tick()
        assert w.detectors[2] is det2 and w.monitor_changes == 1
        assert w.get_latest_frame(1) is not None          # fresh baseline on the new rect

    def test_hotplug_grab_exception_does_not_crash(self):
        # T28: a grab that raises is caught, counted, logged, backs off, then forces re-enumeration
        w, be, logs = self._hotplug({1: R1, 2: R2})
        w.tick()
        be.fail.add(2)
        w.tick()                                          # must not raise
        assert w.status()["errors"] == 1 and any("grab failed on monitor 2" in t for t in logs)
        n = be.refreshes
        w.tick()                                          # inside back-off: nothing happens
        assert be.refreshes == n
        w.clock.advance(sw.ScreenWatch.GRAB_BACKOFF_S + 0.1)
        be.fail.clear(); be.unplug(2)
        w.tick()
        assert be.refreshes == n + 1 and w.monitor_rects() == {1: R1}

    def test_hotplug_config_points_at_vanished_monitor(self):
        # T29: screen_watch_monitors [2] with monitor 2 unplugged -> no grabs, no errors, no crash
        w, be, _ = self._hotplug({1: R1, 2: R2}, {"screen_watch_monitors": [2]})
        w.tick()
        be.unplug(2)
        n = len(be.grabbed)
        w.tick()
        assert len(be.grabbed) == n and w.status()["errors"] == 0

    def test_hotplug_ring_wiped_for_vanished_monitor(self):
        # T30: kept frames of the unplugged monitor are zeroed and dropped, others kept
        w, be, _ = self._hotplug({1: R1, 2: R2})
        w.tick()
        buf = next(it[2] for it in w.ring._items if it[0] == 2)
        be.unplug(2)
        w.tick()
        assert w.get_latest_frame(2) is None and w.get_latest_frame(1) is not None
        assert not any(buf)

    def test_hotplug_periodic_recheck_without_signal(self):
        # T31: no signal change -> still re-enumerates every RECHECK_EVERY passes
        w, be, _ = self._hotplug({1: R1})
        be.sig_fixed = True
        w.tick()
        be.plug(2, R2)
        for _ in range(sw.ScreenWatch.RECHECK_EVERY):
            w.tick()
        assert 2 in w.monitor_rects()

    # ------------------------------------------------------------------ real capture smoke
    def test_real_capture_smoke(self):
        # T24: one real mss grab per monitor, kept in RAM only; skipped if capture is unavailable
        try:
            be = sw.MssBackend()
            mons = be.monitors()
            f = be.grab(mons[0])
        except Exception as e:
            self.skipTest(f"capture unavailable: {e}")
        try:
            assert f.ndim == 3 and f.shape[2] == 4
            assert sw.encode_jpeg(f)[:2] == b"\xff\xd8"
        finally:
            be.close()

    # ------------------------------------------------------------------ fallback chain / log throttling
    def test_fallback_uses_alternate_when_primary_fails_and_retries_primary(self):
        clock = FakeClock()
        prim = HotplugBackend({1: R1})
        prim.frames[1] = frame(50, 360, 640)
        prim.fail.add(1)

        class Alt:
            grabs = 0
            def __init__(self, layout_fn): pass
            def grab(self, mon):
                Alt.grabs += 1
                return frame(90, 360, 640)
            def close(self): pass

        fb = sw.FallbackBackend(lambda: prim, [("missing", lambda lf: 1 / 0), ("alt", Alt)], clock=clock)
        assert int(fb.grab(1)[0, 0, 0]) == 90 and fb.using[1] == "alt"
        fb.grab(1)                                   # sticks with alt, primary not retried yet
        assert prim.grabbed == []
        prim.fail.clear()
        clock.advance(fb.PRIMARY_RETRY_S + 1)
        assert int(fb.grab(1)[0, 0, 0]) == 50 and 1 not in fb.using

    def test_fallback_skips_black_frames_but_returns_black_if_all_black(self):
        prim = HotplugBackend({1: R1})                # frames are all zero (black)

        class Alt:
            def __init__(self, lf): pass
            def grab(self, mon): return frame(40, 360, 640)
            def close(self): pass

        fb = sw.FallbackBackend(lambda: prim, [("alt", Alt)], clock=FakeClock())
        assert int(fb.grab(1)[0, 0, 0]) == 40
        fb2 = sw.FallbackBackend(lambda: HotplugBackend({1: R1}), [], clock=FakeClock())
        assert int(fb2.grab(1).max()) == 0

    def test_fallback_raises_when_everything_fails(self):
        prim = HotplugBackend({1: R1})
        prim.fail.add(1)
        fb = sw.FallbackBackend(lambda: prim, [("missing", lambda lf: 1 / 0)], clock=FakeClock())
        with self.assertRaises(OSError):
            fb.grab(1)

    def test_grab_failure_log_is_rate_limited_with_detail_and_recovery(self):
        clock, logs = FakeClock(), []
        be = HotplugBackend({1: R1})
        be.fail.add(1)
        w = sw.ScreenWatch(ON, backend_factory=lambda: be, clock=clock, log=lambda who, t: logs.append(t))
        for _ in range(40):                          # 40 failures over ~100 s
            w.tick()
            clock.advance(sw.ScreenWatch.GRAB_BACKOFF_S + 0.5)
        fails = [t for t in logs if "fail" in t]
        assert len(fails) == 2, fails                # first + one per minute
        assert "gdi grab failed" in fails[0] and "more failures" in fails[1]
        be.fail.clear()
        w.tick()
        assert any("capturing again" in t and "40 failures" in t for t in logs)

    def test_fullscreen_pause_off_by_default(self):
        w = sw.ScreenWatch({"screen_watch_enabled": True}, backend_factory=lambda: FakeBackend({1: frame()}),
                           clock=FakeClock())
        assert w.guard_fn is None


if __name__ == "__main__":
    unittest.main()
