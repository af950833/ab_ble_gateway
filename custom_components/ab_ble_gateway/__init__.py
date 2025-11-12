from __future__ import annotations
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from .const import (
    DOMAIN,
    CONF_MQTT_TOPIC,
    DEFAULT_MQTT_TOPIC,
    CONF_IDLE_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
)
from .mqtt_client import AbBleMqtt

PLATFORMS = ["device_tracker"]

async def _update_listener(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    data = entry.data
    opts = entry.options

    topic = data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC)
    idle = opts.get(CONF_IDLE_TIMEOUT, data.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT))

    client = AbBleMqtt(hass, topic, idle)
    await client.async_start()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_update_listener))

    def _sweep(now):
        client.sweep_stale()
    entry.async_on_unload(async_track_time_interval(hass, _sweep, timedelta(seconds=max(10, idle//2))))

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    client: AbBleMqtt = hass.data[DOMAIN].pop(entry.entry_id)
    await client.async_stop()
    return ok
