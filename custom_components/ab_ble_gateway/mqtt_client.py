from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
import hmac
import json
import logging
import re
from typing import Dict, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.components import mqtt
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, DEFAULT_MQTT_TOPIC, EDS_HEADER, IBC_HEADER

SIGNAL_PACKET = f"{DOMAIN}_packet"
_LOGGER = logging.getLogger(f"custom_components.{DOMAIN}")

try:
    from bluetooth_data_tools import get_cipher_for_irk, resolve_private_address
except Exception:  # pragma: no cover - depends on the HA bluetooth runtime
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def get_cipher_for_irk(irk: bytes) -> Cipher:
            return Cipher(algorithms.AES(irk), modes.ECB())

        def resolve_private_address(cipher: Cipher, address: str) -> bool:
            rpa = binascii.unhexlify(address.replace(":", ""))
            if len(rpa) != 6 or rpa[0] & 0xC0 != 0x40:
                return False
            encryptor = cipher.encryptor()
            ct = encryptor.update(b"\x00" * 13 + rpa[:3]) + encryptor.finalize()
            return hmac.compare_digest(ct[13:], rpa[3:])

    except Exception:  # pragma: no cover - only if cryptography is unavailable
        get_cipher_for_irk = None
        resolve_private_address = None

HEX32_RE = re.compile(r"^[0-9A-F]{32}$", re.I)

@dataclass
class Packet:
    key: str
    mac: str
    rssi: Optional[int]
    adv: str
    ts: float

class AbBleMqtt:
    def __init__(self, hass: HomeAssistant, topic: str, idle_timeout: int):
        self.hass = hass
        self.topic = topic or DEFAULT_MQTT_TOPIC
        self.idle_timeout = idle_timeout
        self._last_seen: Dict[str, float] = {}
        self._irk_ciphers: Dict[str, object] = {}
        self._unsub = None

    async def async_start(self):
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise RuntimeError("MQTT integration not available")
        self._unsub = await mqtt.async_subscribe(self.hass, self.topic, self._msg, 0)

    async def async_stop(self):
        if self._unsub:
            self._unsub()
            self._unsub = None

    def last_seen(self, key: str) -> Optional[float]:
        return self._last_seen.get(key)

    def set_irks(self, irks: dict[str, bytes]) -> None:
        self._irk_ciphers.clear()
        if not irks:
            return
        if get_cipher_for_irk is None or resolve_private_address is None:
            _LOGGER.warning("bluetooth_data_tools is not available; IRK matching is disabled")
            return
        for key, irk in irks.items():
            try:
                self._irk_ciphers[key] = get_cipher_for_irk(irk)
            except Exception as err:
                _LOGGER.warning("Failed to load IRK %s: %s", key, err)

    def sweep_stale(self):
        self.hass.loop.call_soon_threadsafe(async_dispatcher_send, self.hass, SIGNAL_PACKET, None)

    @callback
    async def _msg(self, msg):
        try:
            raw = msg.payload
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "ignore")
            payload = json.loads(raw)
            devices = payload.get("devices", [])
        except Exception:
            return
        now = datetime.now(timezone.utc).timestamp()
        for raw in devices:
            try:
                mac = str(raw[1]).upper()
                rssi = int(raw[2]) if raw[2] is not None else None
                adv = str(raw[3]).upper()
            except Exception:
                continue

            key = self._parse_key(mac, adv)

            try:
                _LOGGER.debug("parsed key=%s mac=%s adv=%s", key, mac, adv)
            except Exception:
                pass

            self._last_seen[key] = now

            pkt = Packet(key=key, mac=mac, rssi=rssi, adv=adv, ts=now)
            async_dispatcher_send(self.hass, SIGNAL_PACKET, pkt)

    def _parse_key(self, mac: str, adv: str) -> str:
        irk_key = self._resolve_irk_key(mac)
        if irk_key:
            return irk_key
        if EDS_HEADER in adv:
            start = adv.find(EDS_HEADER) + len(EDS_HEADER)
            return ("EDS_" + adv[start+4:start+24]).upper()
        if IBC_HEADER in adv:
            start = adv.find(IBC_HEADER) + len(IBC_HEADER)
            uuid32 = adv[start:start+32]
            major4 = adv[start+32:start+36]
            minor4 = adv[start+36:start+40]
            return ("IBC_" + (uuid32 + major4 + minor4)).upper()
        return mac

    def _resolve_irk_key(self, mac: str) -> str | None:
        if not self._irk_ciphers or resolve_private_address is None:
            return None
        address = _format_mac(mac)
        if not address:
            return None
        for key, cipher in self._irk_ciphers.items():
            try:
                if resolve_private_address(cipher, address):
                    return key
            except Exception as err:
                _LOGGER.debug("IRK resolve failed for %s: %s", address, err)
        return None


def _format_mac(mac: str) -> str:
    clean = re.sub(r"[^0-9A-F]", "", mac.upper())
    if len(clean) != 12:
        return ""
    return ":".join(clean[i:i+2] for i in range(0, 12, 2))


def parse_irk_value(value: str) -> bytes | None:
    raw = value.strip()
    if raw.lower().startswith("irk:"):
        raw = raw[4:]
    if not raw:
        return None
    if HEX32_RE.fullmatch(raw):
        try:
            return binascii.unhexlify(raw)
        except binascii.Error:
            return None
    try:
        decoded = base64.b64decode(raw, validate=True)
    except binascii.Error:
        return None
    if len(decoded) != 16:
        return None
    return bytes(reversed(decoded))
