import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault(
    "FUBON_AGENT_CONF",
    os.path.join(os.path.dirname(__file__), "fixtures", "test_settings.yaml"),
)


class _Enum:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"<{self.name}>"


class _BSAction:
    Buy = _Enum("Buy")
    Sell = _Enum("Sell")


class _FutOptMarketType:
    Future = _Enum("Future")


class _FutOptPriceType:
    Market = _Enum("Market")
    Limit = _Enum("Limit")


class _TimeInForce:
    ROD = _Enum("ROD")
    IOC = _Enum("IOC")
    FOK = _Enum("FOK")


class _FutOptOrderType:
    Auto = _Enum("Auto")


class _FubonSDK:
    """Placeholder — real behavior is stubbed per-test via mock.patch."""

    def login(self, *args, **kwargs):
        raise NotImplementedError("stub in individual tests")


class _FutOptOrder:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _install_fake_fubon_neo():
    if "fubon_neo" in sys.modules:
        return

    fubon_neo = MagicMock(name="fubon_neo")
    fubon_neo_sdk = MagicMock(name="fubon_neo.sdk")
    fubon_neo_constant = MagicMock(name="fubon_neo.constant")

    fubon_neo_sdk.FubonSDK = _FubonSDK
    fubon_neo_sdk.FutOptOrder = _FutOptOrder

    fubon_neo_constant.BSAction = _BSAction
    fubon_neo_constant.FutOptMarketType = _FutOptMarketType
    fubon_neo_constant.FutOptPriceType = _FutOptPriceType
    fubon_neo_constant.TimeInForce = _TimeInForce
    fubon_neo_constant.FutOptOrderType = _FutOptOrderType

    fubon_neo.sdk = fubon_neo_sdk
    fubon_neo.constant = fubon_neo_constant

    sys.modules["fubon_neo"] = fubon_neo
    sys.modules["fubon_neo.sdk"] = fubon_neo_sdk
    sys.modules["fubon_neo.constant"] = fubon_neo_constant


_install_fake_fubon_neo()
