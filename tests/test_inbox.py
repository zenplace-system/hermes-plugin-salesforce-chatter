import os
import stat
from datetime import datetime, timedelta, timezone

from sfchatter.inbox import Claim, Inbox

T0 = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def claim(source_id="0D7A", feed="0D5A", kind="comment", requester="005U1"):
    return Claim(source_id=source_id, feed_element_id=feed, kind=kind, requester_id=requester)


def test_first_scan_since_is_now_and_db_is_owner_only(tmp_path):
    path = tmp_path / "inbox.sqlite"
    inbox = Inbox(path, clock=Clock(T0))
    assert inbox.scan_since(timedelta(hours=24)) == T0
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_scan_since_is_capped_by_max_catchup(tmp_path):
    clock = Clock(T0)
    inbox = Inbox(tmp_path / "inbox.sqlite", clock=clock)
    inbox.scan_since(timedelta(hours=24))
    clock.now = T0 + timedelta(days=3)
    assert inbox.scan_since(timedelta(hours=24)) == T0 + timedelta(days=2)


def test_advance_scan_keeps_overlap_and_never_moves_back(tmp_path):
    inbox = Inbox(tmp_path / "inbox.sqlite", clock=Clock(T0 + timedelta(hours=1)))
    inbox.scan_since(timedelta(hours=24))
    inbox.advance_scan(T0 + timedelta(hours=2), timedelta(minutes=5))
    assert inbox.scan_since(timedelta(hours=24)) == T0 + timedelta(hours=1, minutes=55)
    inbox.advance_scan(T0, timedelta(minutes=5))
    assert inbox.scan_since(timedelta(hours=24)) == T0 + timedelta(hours=1, minutes=55)


def test_claim_is_idempotent_across_reopen(tmp_path):
    path = tmp_path / "inbox.sqlite"
    first = Inbox(path, clock=Clock(T0))
    assert first.claim(claim())
    first.set_status("0D7A", "liked")
    first.close()
    second = Inbox(path, clock=Clock(T0 + timedelta(minutes=10)))
    assert not second.claim(claim())


def test_answered_in_last_hour_counts_only_recent_answers(tmp_path):
    clock = Clock(T0)
    inbox = Inbox(tmp_path / "inbox.sqlite", clock=clock)
    inbox.claim(claim("0D7A"))
    inbox.set_status("0D7A", "answered")
    clock.now = T0 + timedelta(minutes=30)
    inbox.claim(claim("0D7B"))
    inbox.set_status("0D7B", "answered")
    inbox.claim(claim("0D7C"))
    inbox.set_status("0D7C", "failed")
    clock.now = T0 + timedelta(minutes=70)
    assert inbox.answered_in_last_hour() == 1


def test_pending_requester_returns_latest_open_claim(tmp_path):
    clock = Clock(T0)
    inbox = Inbox(tmp_path / "inbox.sqlite", clock=clock)
    inbox.claim(claim("0D7A", requester="005U1"))
    inbox.set_status("0D7A", "answered")
    clock.now = T0 + timedelta(minutes=1)
    inbox.claim(claim("0D7B", requester="005U2"))
    assert inbox.pending_requester("0D5A") == "005U2"
    inbox.set_status("0D7B", "answered")
    assert inbox.pending_requester("0D5A") is None
