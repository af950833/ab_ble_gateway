from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Dict, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.components import mqtt
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, DEFAULT_MQTT_TOPIC, EDS_HEADER, IBC_HEADER

SIGNAL_PACKET = f"{DOMAIN}_packet"

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
                import logging
                logging.getLogger(f"custom_components.{DOMAIN}").debug("parsed key=%s adv=%s", key, adv)
            except Exception:
                pass

            self._last_seen[key] = now

            pkt = Packet(key=key, mac=mac, rssi=rssi, adv=adv, ts=now)
            async_dispatcher_send(self.hass, SIGNAL_PACKET, pkt)

    def _parse_key(self, mac: str, adv: str) -> str:
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
