"""Finding and addressing the Samsung TV — the pure half of lib/samsung.

The device description here is the real one this TV answered with on 2026-08-14 (trimmed
to the fields anything reads), which is also where config.SAMSUNG_MAC came from.
"""

import asyncio

import pytest

from lib.platform import net
from lib.samsung import control

DESCRIPTION = {
    "device": {
        "PowerState": "on",
        "TokenAuthSupport": "true",
        "modelName": "GQ55Q80DATXZG",
        "name": "TV Samsung",
        "networkType": "wired",
        "type": "Samsung SmartTV",
        "wifiMac": "02:5a:00:01:02:03",
    },
    "name": "TV Samsung",
}

OTHER_TV = {"device": dict(DESCRIPTION["device"], wifiMac="aa:bb:cc:dd:ee:ff",
                           modelName="UE40H7000")}

ARP = """\
? (192.168.0.1) at 0:66:77:88:99:aa on en1 ifscope [ethernet]
? (192.168.0.42) at 2:5a:0:1:2:3 on en1 ifscope [ethernet]
? (192.168.0.187) at 0:66:77:88:99:aa on en1 permanent [ethernet]
? (224.0.0.251) at 1:0:5e:0:0:fb on en1 ifscope permanent [ethernet]
"""


def test_arp_is_read_through_the_leading_zeros_macos_strips():
    """`arp -an` prints 00:7d:… as 0:7d:…, so a config MAC never matches literally."""
    found = net.ips_by_mac(ARP)
    assert found[net.norm_mac("02:5a:00:01:02:03")] == "192.168.0.42"
    assert net.norm_mac("02:AD:BE:0D:EE:01") == "2:ad:be:d:ee:1"


def test_subnet_hosts_covers_the_24_and_leaves_us_out():
    hosts = net.subnet_hosts("192.168.0.23")
    assert len(hosts) == 253
    assert "192.168.0.1" in hosts and "192.168.0.254" in hosts
    assert "192.168.0.23" not in hosts
    assert net.subnet_hosts(None) == []


def test_the_tv_is_recognised_by_its_own_mac():
    assert control.is_ours(DESCRIPTION)
    assert not control.is_ours(OTHER_TV)
    assert not control.is_ours(None)
    assert control.is_a_samsung_tv(OTHER_TV)  # still a Samsung, just not ours


def test_the_remote_url_carries_the_name_and_only_the_token_it_has():
    bare = control.remote_url("192.168.0.42")
    assert bare.startswith("wss://192.168.0.42:8002/api/v2/channels/samsung.remote.control")
    assert "name=Z2lnYWt1" in bare  # base64("gigaku") — half of what the token is bound to
    assert "token=" not in bare
    assert control.remote_url("192.168.0.42", "60732690").endswith("&token=60732690")


def test_a_key_is_one_click_of_that_key():
    import json

    msg = json.loads(control.key_message(control.VOLUME_UP))
    assert msg["method"] == "ms.remote.control"
    assert msg["params"]["DataOfCmd"] == "KEY_VOLUP"
    assert msg["params"]["Cmd"] == "Click"


@pytest.fixture
def ladder(monkeypatch, tmp_path):
    """Stub out the three rungs of find_tv so each can be made to answer or not."""
    state = {"arp": None, "answers": {}, "scan": [], "scanned": 0}
    monkeypatch.setattr(control, "SAMSUNG_IP_CACHE", str(tmp_path / "tv-ip"))
    monkeypatch.setattr(control.net, "ip_for_mac", lambda _mac: state["arp"])

    async def describe(_session, ip, timeout=None):
        return state["answers"].get(ip)

    async def scan(_session, _log):
        state["scanned"] += 1
        return state["scan"]

    monkeypatch.setattr(control, "describe", describe)
    monkeypatch.setattr(control, "_scan", scan)
    return state


def test_the_arp_entry_wins_and_is_published(ladder, tmp_path):
    ladder["arp"] = "192.168.0.42"
    ladder["answers"] = {"192.168.0.42": DESCRIPTION}
    assert asyncio.run(control.find_tv(None, log=lambda _m: None)) == "192.168.0.42"
    assert (tmp_path / "tv-ip").read_text() == "192.168.0.42"
    assert ladder["scanned"] == 0  # nothing scans while the ARP table knows


def test_the_cache_answers_when_the_arp_entry_has_aged_out(ladder, tmp_path):
    (tmp_path / "tv-ip").write_text("192.168.0.42")
    ladder["answers"] = {"192.168.0.42": DESCRIPTION}
    assert asyncio.run(control.find_tv(None, log=lambda _m: None)) == "192.168.0.42"
    assert ladder["scanned"] == 0


def test_a_cache_pointing_at_somebody_else_is_not_believed(ladder, tmp_path):
    """The hardcoded IP this replaced was stale — a cached one goes stale the same way."""
    (tmp_path / "tv-ip").write_text("192.168.0.198")
    ladder["answers"] = {"192.168.0.198": OTHER_TV, "192.168.0.42": DESCRIPTION}
    ladder["scan"] = [("192.168.0.42", DESCRIPTION)]
    assert asyncio.run(control.find_tv(None, log=lambda _m: None)) == "192.168.0.42"
    assert ladder["scanned"] == 1
    assert (tmp_path / "tv-ip").read_text() == "192.168.0.42"


def test_the_scan_takes_a_lone_samsung_whose_mac_it_doesnt_know(ladder):
    """A replaced TV has a new MAC; refusing to find it is the worse failure."""
    ladder["scan"] = [("192.168.0.51", OTHER_TV)]
    said = []
    assert asyncio.run(control.find_tv(None, log=said.append)) == "192.168.0.51"
    assert any("UE40H7000" in line for line in said)  # and it says so


def test_two_strange_samsungs_are_refused_rather_than_guessed(ladder):
    ladder["scan"] = [("192.168.0.51", OTHER_TV), ("192.168.0.52", OTHER_TV)]
    with pytest.raises(control.SamsungTVError):
        asyncio.run(control.find_tv(None, log=lambda _m: None))


def test_no_tv_anywhere_says_so(ladder):
    with pytest.raises(control.SamsungTVError, match="No Samsung TV"):
        asyncio.run(control.find_tv(None, log=lambda _m: None))
