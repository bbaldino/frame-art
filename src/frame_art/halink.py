"""The link to Home Assistant, over MQTT. HA stays the only thing talking to the TV.

We publish one MQTT-discovered sensor, ``sensor.frame_art``. Its state is a
sequence number that goes up whenever there is new work for HA (new files to
upload, TV content ids to delete); its attributes say what that work is. The HA
automation triggers on it and reports back on ``frame-art/ack``:

    {"deleted": "MY_F0123"}                 one TV id it deleted
    {"upload": {...art_upload_batch response...}}

A second HA automation forwards the TV's current artwork id on
``frame-art/current`` ({"content_id": "MY_F0123"}), and we answer with
``sensor.frame_art_now_showing``: the work's title, with artist, date, museum,
description and so on as attributes (empty when the TV shows something else).

The web UI's TV buttons publish requests on ``frame-art/command`` (not
retained, so a restart never replays one):

    {"id": 1761512345000, "action": "upload"}
    {"id": 1761512345001, "action": "mine", "duration": "1h"}
    {"id": 1761512345002, "action": "samsung", "duration": "1h"}

A third HA automation carries them out and answers on ``frame-art/ack`` with
{"command": {"id": ..., "ok": true}} or {"command": {"id": ..., "error": "..."}}.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)

STATE = "frame-art/state"
STATUS = "frame-art/status"
ACK = "frame-art/ack"
CURRENT = "frame-art/current"
NOW = "frame-art/now"
COMMAND = "frame-art/command"
DISCOVERY = "homeassistant/sensor/frame_art/state/config"
NOW_DISCOVERY = "homeassistant/sensor/frame_art/now_showing/config"


class HaLink:
    def __init__(
        self,
        host: str,
        port: int,
        on_ack: Callable[[dict], None],
        version: str,
        on_current: Callable[[dict], None] | None = None,
    ):
        self.on_ack = on_ack
        self.on_current = on_current
        self.version = version
        self._last: dict | None = None
        self._last_now: dict | None = None
        self._lock = threading.Lock()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="frame-art", clean_session=True
        )
        self.client.will_set(STATUS, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 60)
        self.client.connect_async(host, port, keepalive=60)
        self.client.loop_start()

    @property
    def connected(self) -> bool:
        return self.client.is_connected()

    def _on_connect(self, client, userdata, flags, reason, properties) -> None:
        if reason.is_failure:
            log.warning("mqtt: connect failed: %s", reason)
            return
        log.info("mqtt: connected")
        client.publish(DISCOVERY, json.dumps(self._discovery()), qos=1, retain=True)
        client.publish(
            NOW_DISCOVERY, json.dumps(self._now_discovery()), qos=1, retain=True
        )
        client.publish(STATUS, "online", qos=1, retain=True)
        client.subscribe([(ACK, 1), (CURRENT, 1)])
        with self._lock:
            if self._last is not None:
                client.publish(STATE, json.dumps(self._last), qos=1, retain=True)
            if self._last_now is not None:
                client.publish(NOW, json.dumps(self._last_now), qos=1, retain=True)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload)
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except ValueError as ex:
            log.warning("mqtt: ignoring malformed ack %r: %s", msg.payload[:200], ex)
            return
        handler = self.on_current if msg.topic == CURRENT else self.on_ack
        if handler is None:
            return
        try:
            handler(payload)
        except Exception:
            log.exception("mqtt: handling %s failed", msg.topic)

    def _discovery(self) -> dict:
        return {
            "name": None,  # the entity takes the device's name: "Frame Art"
            "unique_id": "frame_art_state",
            "default_entity_id": "sensor.frame_art",
            "state_topic": STATE,
            "value_template": "{{ value_json.seq }}",
            "json_attributes_topic": STATE,
            "availability_topic": STATUS,
            "icon": "mdi:palette",
            "device": {
                "identifiers": ["frame_art"],
                "name": "Frame Art",
                "manufacturer": "frame-art",
                "sw_version": self.version,
            },
        }

    def _now_discovery(self) -> dict:
        return {
            "name": "Now showing",
            "unique_id": "frame_art_now_showing",
            "default_entity_id": "sensor.frame_art_now_showing",
            "state_topic": NOW,
            # Entity states are capped at 255 characters; titles rarely come close.
            "value_template": "{{ (value_json.title or 'Not from frame-art')[:255] }}",
            "json_attributes_topic": NOW,
            "availability_topic": STATUS,
            "icon": "mdi:image-frame",
            "device": {"identifiers": ["frame_art"]},
        }

    def publish_now(self, info: dict) -> None:
        """Publish (retained) what the TV is showing."""
        with self._lock:
            self._last_now = info
        if self.connected:
            self.client.publish(NOW, json.dumps(info), qos=1, retain=True)

    def publish(self, state: dict) -> None:
        """Publish (retained) what HA should do next."""
        with self._lock:
            self._last = state
        if self.connected:
            self.client.publish(STATE, json.dumps(state), qos=1, retain=True)

    def command(self, payload: dict) -> bool:
        """Ask HA to do something to the TV; False if the request can't be sent."""
        if not self.connected:
            return False
        info = self.client.publish(COMMAND, json.dumps(payload), qos=1)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    def close(self) -> None:
        self.client.publish(STATUS, "offline", qos=1, retain=True)
        self.client.loop_stop()
        self.client.disconnect()
