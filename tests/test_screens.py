"""Session C1d: screens.py, pure. Thumbnail arithmetic, the distance and its
thresholds (the probe's numbers), the hysteresis on synthetic thumbnail
streams (a single noisy sample never toggles a screen; the band between match
and miss continues neither run), learning's stable capture, and the PNG the
expanded pill will show."""

import random
import unittest
import zlib

from peep import screens as s

W, H = 16, 10


def thumb(seed: int, offset: int = 0, noise: int = 0, rnd=None) -> bytes:
    r = random.Random(seed)
    out = []
    for _ in range(W * H):
        v = r.randrange(256) + offset + ((rnd or r).randint(-noise, noise) if noise else 0)
        out.append(min(255, max(0, v)))
    return bytes(out)


def shifted(base: bytes, n_changed: int, delta: int) -> bytes:
    """`base` with its first n_changed pixels moved by delta (a hover effect, a clock)."""
    b = bytearray(base)
    for i in range(n_changed):
        b[i] = b[i] + delta if b[i] + delta <= 255 else b[i] - delta       # always exactly |delta| apart
    return bytes(b)


class ThumbnailTest(unittest.TestCase):
    def test_thumb_size_follows_the_screen(self):
        self.assertEqual(s.thumb_size(2560, 1600), (64, 40))
        self.assertEqual(s.thumb_size(1920, 1080), (64, 36))
        self.assertEqual(s.thumb_size(2560, 1600, 32), (32, 20))
        with self.assertRaises(ValueError):
            s.thumb_size(0, 1600)

    def test_grey_is_the_same_formula_from_gdi_and_from_ffmpeg(self):
        bgra = bytes([0, 140, 255, 255, 10, 20, 30, 0])          # B G R A
        rgb = bytes([255, 140, 0, 30, 20, 10])                     # R G B
        self.assertEqual(s.grey_from_bgra(bgra), s.grey_from_rgb(rgb))
        self.assertEqual(s.grey_from_rgb(bytes([200, 200, 200])), bytes([200]))     # grey stays grey
        self.assertEqual(s.grey_from_rgb(bytes([255, 255, 255, 0, 0, 0])), bytes([255, 0]))

    def test_distance(self):
        a = thumb(1)
        self.assertEqual(s.distance(a, a), s.Distance(0.0, 0.0))
        d = s.distance(a, shifted(a, 16, 100))                     # 10 % of the pixels moved a lot
        self.assertAlmostEqual(d.changed_pct, 10.0)
        self.assertAlmostEqual(d.mad, 10.0)
        with self.assertRaises(ValueError):
            s.distance(a, a[:-1])                                  # another screen size: a bug, said

    def test_the_probes_numbers_sit_on_the_right_side_of_the_defaults(self):
        thr = s.Thresholds()
        hover = s.Distance(4.9, 3.0)               # same page, mouse moving: matches
        scroll = s.Distance(10.5, 17.0)            # same page scrolled three lines: a clear miss
        other = s.Distance(90.0, 71.0)             # another page
        cross = s.Distance(3.5, 1.0)               # GDI vs ddagrab, same page
        self.assertTrue(s.is_match(hover, thr) and s.is_match(cross, thr))
        self.assertTrue(s.is_miss(scroll, thr) and s.is_miss(other, thr))
        self.assertFalse(s.is_match(scroll, thr))
        band = s.Distance(10.0, 10.0)              # between: neither
        self.assertFalse(s.is_match(band, thr) or s.is_miss(band, thr))

    def test_thresholds_round_trip_validate_and_loosen(self):
        thr = s.Thresholds(match_mad=7.0, hysteresis=3)
        self.assertEqual(s.Thresholds.from_dict(thr.to_dict()), thr)
        self.assertEqual(s.Thresholds.from_dict({"bogus": 1, "match_mad": "x"}), s.Thresholds())
        self.assertEqual(s.Thresholds.from_dict(None), s.Thresholds())
        s.Thresholds().validate()
        for bad in (s.Thresholds(match_mad=20.0), s.Thresholds(match_changed_pct=0.0), s.Thresholds(hysteresis=0),
                    s.Thresholds(changed_level=0)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bad.validate()
        loose = thr.loosened(6.0, 6.0)
        self.assertEqual((loose.match_mad, loose.match_changed_pct, loose.hysteresis), (13.0, 14.0, 3))

    def test_same_screen(self):
        thr = s.Thresholds()
        a = thumb(1)
        self.assertTrue(s.same_screen(a, shifted(a, 3, 40), thr))
        self.assertFalse(s.same_screen(a, thumb(2), thr))
        self.assertFalse(s.same_screen(a, a[:-10], thr))           # different sizes are never the same


class PresenceTest(unittest.TestCase):
    """Hysteresis on synthetic streams: 4 samples a second, the screen `ref`."""

    def run_stream(self, frames, present=False, thr=None):
        p = s.Presence(REF, thr or s.Thresholds(), present=present)
        out = []
        for i, f in enumerate(frames):
            ch = p.feed(100.0 + i * 0.25, f)
            if ch:
                out.append((ch["change"], ch["qpc"]))
        return out, p

    def test_appears_after_two_matches_stamped_with_the_first(self):
        changes, p = self.run_stream([OTHER, OTHER, REF, REF, REF])
        self.assertEqual(changes, [("appear", 100.5)])
        self.assertTrue(p.present)

    def test_goes_after_two_misses_stamped_with_the_first(self):
        changes, _ = self.run_stream([REF, REF, REF, OTHER, OTHER, OTHER])
        self.assertEqual(changes, [("appear", 100.0), ("gone", 100.75)])

    def test_a_single_noisy_sample_never_toggles(self):
        # one stray match while absent, one stray miss while present: nothing
        changes, _ = self.run_stream([OTHER, REF, OTHER, OTHER])
        self.assertEqual(changes, [])
        changes, _ = self.run_stream([REF, OTHER, REF, REF], present=True)
        self.assertEqual(changes, [])

    def test_hover_and_a_ticking_clock_keep_matching(self):
        rnd = random.Random(5)
        noisy = [shifted(REF, 4, rnd.choice((40, 60))) for _ in range(20)]     # 2.5 % of the pixels flip
        changes, p = self.run_stream(noisy, present=True)
        self.assertEqual(changes, [])
        self.assertTrue(p.present)

    def test_the_band_continues_neither_run(self):
        band = shifted(REF, 16, 70)                    # 10 % changed: not a match, not a clear miss
        d = s.distance(band, REF)
        thr = s.Thresholds()
        self.assertFalse(s.is_match(d, thr) or s.is_miss(d, thr), d)
        changes, _ = self.run_stream([band] * 6)              # absent: band never appears
        self.assertEqual(changes, [])
        changes, _ = self.run_stream([band] * 6, present=True)  # present: band never goes
        self.assertEqual(changes, [])
        # a band sample breaks a run: miss, band, miss, miss -> gone at the third
        changes, _ = self.run_stream([OTHER, band, OTHER, OTHER], present=True)
        self.assertEqual(changes, [("gone", 100.5)])

    def test_a_larger_hysteresis_waits_longer(self):
        changes, _ = self.run_stream([REF, REF, REF], thr=s.Thresholds(hysteresis=3))
        self.assertEqual(changes, [("appear", 100.0)])
        changes, _ = self.run_stream([REF, REF], thr=s.Thresholds(hysteresis=3))
        self.assertEqual(changes, [])

    def test_random_streams_toggle_only_on_runs(self):
        rnd = random.Random(1008)
        for case in range(200):
            frames, truth = [], []
            for _ in range(rnd.randint(5, 40)):
                on = rnd.random() < 0.5
                frames.append(REF if on else OTHER)
                truth.append(on)
            changes, p = self.run_stream(frames)
            with self.subTest(case=case):
                state = False
                for i in range(len(truth)):
                    # a change at sample i requires `hysteresis` agreeing samples starting at i
                    for kind, q in changes:
                        if abs(q - (100.0 + i * 0.25)) < 1e-9:
                            want = kind == "appear"
                            self.assertTrue(all(t == want for t in truth[i:i + 2]), (i, truth))
                            self.assertNotEqual(state, want)
                            state = want
                self.assertEqual(state, p.present)


REF = thumb(11)
OTHER = thumb(12)


class StableCaptureTest(unittest.TestCase):
    def test_waits_until_two_samples_agree(self):
        seq = iter([thumb(1), thumb(2), thumb(3), thumb(3)])
        slept = []
        got, stable, waited = s.stable_capture(lambda: next(seq), s.Thresholds(), wait_s=1.0, step_s=0.1,
                                               sleep=slept.append)
        self.assertEqual((got, stable), (thumb(3), True))
        self.assertAlmostEqual(waited, 0.3)

    def test_never_settles_uses_the_last_and_says_so(self):
        n = iter(range(100))
        got, stable, waited = s.stable_capture(lambda: thumb(next(n)), s.Thresholds(), wait_s=0.5, step_s=0.1,
                                               sleep=lambda x: None)
        self.assertFalse(stable)
        self.assertAlmostEqual(waited, 0.5)
        self.assertEqual(got, thumb(5))

    def test_no_wait(self):
        got, stable, waited = s.stable_capture(lambda: thumb(7), s.Thresholds(), wait_s=0.0, sleep=lambda x: None)
        self.assertEqual((got, stable, waited), (thumb(7), False, 0.0))


class PngTest(unittest.TestCase):
    def test_round_trip(self):
        data = s.png_grey(W, H, REF)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(s.read_png_grey(data), (W, H, REF))

    def test_a_64x40_thumbnail_is_small(self):
        big = bytes(range(256)) * 10
        self.assertLess(len(s.png_grey(64, 40, big)), 4000)

    def test_refuses_what_it_did_not_write(self):
        with self.assertRaises(ValueError):
            s.read_png_grey(b"GIF89a")
        with self.assertRaises(ValueError):
            s.png_grey(4, 4, b"\0" * 15)
        rgb = bytearray(s.png_grey(2, 2, b"\0" * 4))
        rgb[8 + 8 + 9] = 2                                   # colour type 2 (RGB) in IHDR
        crc = zlib.crc32(bytes(rgb[12:29])) & 0xFFFFFFFF
        rgb[29:33] = crc.to_bytes(4, "big")
        with self.assertRaisesRegex(ValueError, "greyscale"):
            s.read_png_grey(bytes(rgb))

    def test_hex(self):
        self.assertEqual(s.from_hex(s.to_hex(REF), W, H), REF)
        with self.assertRaises(ValueError):
            s.from_hex(s.to_hex(REF), W, H + 1)


if __name__ == "__main__":
    unittest.main()
