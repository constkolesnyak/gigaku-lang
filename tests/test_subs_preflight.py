"""What `gigaku subs` refuses before it starts, and whether the reason it names is the real one.

`proFeaturesEnabled: false` has two causes with two different fixes — no subscription, and a
Language Reactor that is simply signed out — and the gate used to print only the first. On
2026-08-20 that refused a whole season with "Subscribe" at a user whose subscription was live
and who only had to sign in. These pin the two apart, and pin that a working Pro account is
never refused over `licenseStatus`, which reads "ZOMBIE" while Pro is active.

Importing lib.subs.subs costs pyobjc; the browser itself is monkeypatched away.
"""
import pytest

from lib.subs import subs


def _reading(**over):
    """One probe's answer, healthy by default — measured shape, ZOMBIE license included."""
    d = {"pro": True, "signedIn": True, "license": "ZOMBIE", "mmReady": True,
         "title": "Some Show", "audioAvail": ["German"], "subAvail": ["asr:German"],
         "audioOk": True, "subOk": True}
    d.update(over)
    return d


@pytest.fixture
def answers(monkeypatch):
    """Switching LR on is the slow half and not what these are about."""
    monkeypatch.setattr(subs, "_ensure_lln_on", lambda: None)

    def _set(reading):
        monkeypatch.setattr(subs, "_run_main", lambda _body, _r=reading: _r)
    return _set


def _refusal(answers, reading):
    answers(reading)
    with pytest.raises(subs.ExportError) as caught:
        subs._preflight_tracks("German", "German", True)
    return str(caught.value)


def test_a_signed_out_lr_is_named_as_signed_out(answers):
    msg = _refusal(answers, _reading(pro=False, signedIn=False, license="NOT_SIGNED_IN"))
    assert "signed out" in msg
    assert "Subscribe" not in msg  # the wrong advice this whole gate exists to stop giving
    assert "NOT_SIGNED_IN" in msg  # LR's own word for it, so the reading can be checked


def test_the_license_alone_decides_it_when_the_auth_flag_is_unreadable(answers):
    """A store LR has not filled in answers None, not False — the license still names it."""
    msg = _refusal(answers, _reading(pro=False, signedIn=None, license="NOT_SIGNED_IN"))
    assert "signed out" in msg


def test_signed_in_without_pro_still_asks_for_a_subscription(answers):
    msg = _refusal(answers, _reading(pro=False, signedIn=True, license="FREE"))
    assert "Pro is required" in msg
    assert "signed out" not in msg
    assert "FREE" in msg


def test_a_live_subscription_passes_even_though_its_license_reads_zombie(answers):
    """Measured 2026-08-20: signed in, Pro working, `licenseStatus: "ZOMBIE"`. Gating on that
    field rather than on `proFeaturesEnabled` would refuse the account that actually works."""
    answers(_reading())
    subs._preflight_tracks("German", "German", True)  # no refusal


def test_a_non_asr_run_is_never_refused_over_pro(answers):
    """LR signed out still exports the free tracks — refusing those refuses work that succeeds."""
    answers(_reading(pro=False, signedIn=False, license="NOT_SIGNED_IN",
                     subAvail=["subtitles:German"]))
    subs._preflight_tracks("German", "German", False)  # no refusal
