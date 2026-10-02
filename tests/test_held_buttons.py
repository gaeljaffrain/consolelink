"""HeldButtons: a Bump button is held for as long as the page holds it and always released --
the console never releases a button by itself."""
from consolelink import app
from consolelink import protocol as sfl


class FakeLink:
    def __init__(self):
        self.sent = []

    def send_button(self, code, pressed):
        self.sent.append((code, pressed))
        return True


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make():
    link, clock = FakeLink(), Clock()
    return app.HeldButtons(link, clock), link, clock


def test_release_goes_out_as_soon_as_the_page_lets_go():
    """No minimum press: a quick tap is released at once (the console may ignore it)."""
    held, link, clock = make()
    held.press(0)
    clock.advance(0.1)
    held.release(0)
    held.service()
    assert link.sent == [(0, True), (0, False)]
    assert held.held == {}


def test_a_long_hold_is_released_when_the_page_lets_go():
    held, link, clock = make()
    held.press(1)
    for _ in range(8):  # 2 s of keepalives
        clock.advance(0.25)
        held.keepalive(1)
        held.service()
    assert link.sent == [(1, True)]
    held.release(1)
    held.service()
    assert link.sent == [(1, True), (1, False)]


def test_lease_expiry_releases_a_client_that_vanished():
    held, link, clock = make()
    held.press(5)
    clock.advance(app.BUMP_LEASE - 0.01)
    held.service()
    assert link.sent == [(5, True)]
    clock.advance(0.02)
    held.service()
    assert link.sent == [(5, True), (5, False)]


def test_max_hold_releases_a_runaway_client():
    held, link, clock = make()
    held.press(2)
    for _ in range(int(app.BUMP_MAX_HOLD / 0.5) + 2):
        clock.advance(0.5)
        held.keepalive(2)
        held.service()
    assert link.sent == [(2, True), (2, False)]


def test_a_late_keepalive_does_not_repress_a_released_button():
    held, link, clock = make()
    held.press(3)
    clock.advance(0.5)
    held.release(3)
    held.service()
    held.keepalive(3)  # was in flight when the release arrived
    held.service()
    assert link.sent == [(3, True), (3, False)]


def test_repeated_press_is_one_hold_and_buttons_are_independent():
    held, link, clock = make()
    held.press(0)
    held.press(0)
    held.press(1)
    assert link.sent == [(0, True), (1, True)]
    clock.advance(0.5)
    held.release(0)
    held.service()
    assert link.sent[-1] == (0, False) and 1 in held.held


def test_release_all_on_shutdown():
    held, link, clock = make()
    held.press(0)
    held.press(11)
    held.release_all()
    assert sorted(link.sent[2:]) == [(0, False), (11, False)]
    assert held.held == {}
