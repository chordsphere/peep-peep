"""The stop dialog's decision logic (keys -> action) and helpers, and the
pill's pure helpers. The Tk layers over these are smoke-test items."""

import unittest

from peep import dialog as d
from peep import pill


class DecideTest(unittest.TestCase):
    def test_unarmed_keys(self):
        self.assertEqual(d.decide("Return", False), (d.SAVE, False))
        self.assertEqual(d.decide("KP_Enter", False), (d.SAVE, False))
        self.assertEqual(d.decide("Escape", False), (d.KEEP, False))
        self.assertEqual(d.decide("Delete", False), (d.ARM, True))
        for typing in ("a", "BackSpace", "space", "y", "Left"):
            self.assertEqual(d.decide(typing, False), (d.NONE, False), typing)

    def test_delete_needs_one_confirming_keystroke(self):
        self.assertEqual(d.decide("Delete", True), (d.DISCARD, False))
        self.assertEqual(d.decide("y", True), (d.DISCARD, False))
        self.assertEqual(d.decide("Y", True), (d.DISCARD, False))

    def test_anything_else_cancels_the_confirm(self):
        for key in ("Escape", "Return", "n", "a", "BackSpace"):
            self.assertEqual(d.decide(key, True), (d.DISARM, False), key)

    def test_modifier_alone_keeps_the_confirm_armed(self):
        self.assertEqual(d.decide("Shift_L", True), (d.NONE, True))     # on the way to a capital Y
        action, armed = d.decide("Shift_L", True)
        self.assertEqual(d.decide("Y", armed), (d.DISCARD, False))

    def test_sequence_delete_escape_delete_delete(self):
        armed, actions = False, []
        for key in ("Delete", "Escape", "Delete", "Delete"):
            action, armed = d.decide(key, armed)
            actions.append(action)
        self.assertEqual(actions, [d.ARM, d.DISARM, d.ARM, d.DISCARD])


class CollectionPickerTest(unittest.TestCase):
    def test_cycle_wraps_both_ways(self):
        choices = ["bale", "inbox", "test"]
        self.assertEqual(d.cycle("bale", choices, +1), "inbox")
        self.assertEqual(d.cycle("test", choices, +1), "bale")
        self.assertEqual(d.cycle("bale", choices, -1), "test")
        self.assertEqual(d.cycle(" Inbox ", choices, -1), "bale")         # typed with case/space

    def test_cycle_from_a_new_name(self):
        self.assertEqual(d.cycle("brand-new", ["bale", "inbox"], +1), "bale")
        self.assertEqual(d.cycle("brand-new", ["bale", "inbox"], -1), "inbox")
        self.assertEqual(d.cycle("x", [], +1), "x")

    def test_choices_order_unique_and_valid(self):
        self.assertEqual(d.collection_choices("bale", ["test", "bale", "inbox", "Bad Name!"], "inbox"),
                         ["bale", "test", "inbox"])
        self.assertEqual(d.collection_choices("bale", [], "inbox"), ["bale", "inbox"])
        self.assertEqual(len(d.collection_choices("a", [f"c{i}" for i in range(30)], "inbox")), 12)


class ValidationAndStatusTest(unittest.TestCase):
    def test_validate_choice(self):
        choice, err = d.validate_choice("  Bale: pack demo  ", "Bale")
        self.assertIsNone(err)
        self.assertEqual(choice, d.Choice("Bale: pack demo", "bale-pack-demo", "bale"))
        self.assertIn("name:", d.validate_choice("!!!", "inbox")[1])
        self.assertIn("collection", d.validate_choice("ok", "../etc")[1])
        self.assertIsNone(d.validate_choice("ok", "")[0])

    def test_status_line(self):
        self.assertEqual(d.status_line(42.4, 12_900_000, r"C:\v\inbox\x.mp4"),
                         "00:42  ·  12.3 MB  ·  C:\\v\\inbox\\x.mp4")
        self.assertEqual(d.status_line(None, None, "p"), "?  ·  ? MB  ·  p")
        self.assertEqual(d.format_size(2048), "2 KB")
        self.assertEqual(d.format_size(3 * 1024 ** 3), "3.00 GB")

    def test_hint_names_every_key(self):
        for word in ("Enter", "Esc", "Delete"):
            self.assertIn(word, d.HINT)


class PillHelpersTest(unittest.TestCase):
    def test_format_elapsed(self):
        self.assertEqual(pill.format_elapsed(12.7), "00:12")
        self.assertEqual(pill.format_elapsed(600), "10:00")
        self.assertEqual(pill.format_elapsed(3725), "1:02:05")
        self.assertEqual(pill.format_elapsed(None), "00:00")
        self.assertEqual(pill.format_elapsed(-3), "00:00")

    def test_pill_text(self):
        self.assertEqual(pill.pill_text("recording", 12), "● 00:12")
        self.assertEqual(pill.pill_text("starting", None), "● starting…")
        self.assertEqual(pill.pill_text("stopping", 99), "■ saving…")

    def test_positions(self):
        sw, sh, w, h, m = 2560, 1600, 100, 30, 24
        self.assertEqual(pill.overlay_position("top-right", sw, sh, w, h, m), (2436, 24))
        self.assertEqual(pill.overlay_position("top-left", sw, sh, w, h, m), (24, 24))
        self.assertEqual(pill.overlay_position("top-center", sw, sh, w, h, m), (1230, 24))
        self.assertEqual(pill.overlay_position("bottom-right", sw, sh, w, h, m), (2436, 1546))
        self.assertEqual(pill.overlay_position("bottom-left", sw, sh, w, h, m), (24, 1546))
        self.assertEqual(pill.overlay_position("bottom-center", sw, sh, w, h, m), (1230, 1546))

    def test_toast_stacks_inside_the_pill(self):
        self.assertEqual(pill.overlay_position("top-right", 2560, 1600, 100, 30, 24, stack=1), (2436, 62))
        self.assertEqual(pill.overlay_position("bottom-left", 2560, 1600, 100, 30, 24, stack=1), (24, 1508))
        self.assertEqual(pill.overlay_position("top-left", 50, 50, 100, 30, 24), (24, 24))
        self.assertEqual(pill.overlay_position("top-right", 50, 50, 100, 30, 24), (0, 24))   # never off-screen left

    def test_every_position_in_config_is_handled(self):
        from peep.config import PILL_POSITIONS
        for pos in PILL_POSITIONS:
            x, y = pill.overlay_position(pos, 1000, 800, 100, 30, 10)
            self.assertTrue(0 <= x <= 900 and 0 <= y <= 770, pos)


if __name__ == "__main__":
    unittest.main()
