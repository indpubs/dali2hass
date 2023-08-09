# Home Assistant integration via mqtt

import time
import sys
from dataclasses import dataclass, asdict
from typing import Optional
import paho.mqtt.client as mqtt
import json
import logging

log = logging.getLogger(__name__)


@dataclass
class Device:
    identifiers: list[str]
    name: str
    manufacturer: str
    model: Optional[str] = None
    hw_version: Optional[str] = None
    sw_version: Optional[str] = None
    via_device: Optional[str] = None


class Entity:
    def __init__(self, unique_id, name, device, component):
        self.unique_id = unique_id
        self.name = name
        self.device = device
        self.component = component

        self.discovery_payload = {
            "name": self.name,
            "unique_id": self.unique_id,
            "object_id": self.unique_id,
        }
        if device:
            self.discovery_payload["device"] = {
                k: v for k, v in asdict(self.device).items()
                if v is not None}

    def _register(self, hass):
        self._hass = hass


class SensorEntity(Entity):
    def __init__(self, unique_id, name, device, state_topic, device_class=None,
                 enabled_by_default=True, category=None):
        super().__init__(unique_id, name, device, "sensor")
        self.state_topic = state_topic
        self.state = None
        self.discovery_payload.update({
            "device_class": device_class,
            "enabled_by_default": enabled_by_default,
            "state_topic": self.state_topic,
            "entity_category": category,
        })

    def set_state(self, state):
        prev_state = self.state
        self.state = state
        if hasattr(self, "_hass") and self.state != prev_state:
            self._hass.update(self)


class BinarySensorEntity(Entity):
    def __init__(self, unique_id, name, device, state_topic, device_class=None,
                 enabled_by_default=True, category=None):
        super().__init__(unique_id, name, device, "binary_sensor")
        self.state_topic = state_topic
        self.state = None
        self.discovery_payload.update({
            "device_class": device_class,
            "enabled_by_default": enabled_by_default,
            "state_topic": self.state_topic,
            "entity_category": category,
        })

    def set_state(self, state):
        prev_state = self.state
        self.state = "ON" if state else "OFF"
        if hasattr(self, "_hass") and self.state != prev_state:
            self._hass.update(self)


class ButtonEntity(Entity):
    def __init__(self, unique_id, name, device, command_topic, callback,
                 device_class=None, enabled_by_default=True, category=None):
        super().__init__(unique_id, name, device, "button")
        self.command_topic = command_topic
        self.callback = callback
        self.discovery_payload.update({
            "device_class": device_class,
            "enabled_by_default": enabled_by_default,
            "command_topic": self.command_topic,
            "entity_category": category,
        })

    def command(self, payload):
        self.callback()


class SceneEntity(Entity):
    def __init__(self, unique_id, name, command_topic, callback,
                 enabled_by_default=True, category=None):
        super().__init__(unique_id, name, None, "scene")
        self.command_topic = command_topic
        self.callback = callback
        self.discovery_payload.update({
            "enabled_by_default": enabled_by_default,
            "command_topic": self.command_topic,
            "payload_on": "ON",
        })
        # Setting category=None is now rejected by Home Assistant. Don't
        # set it at all for primary entities.
        if category:
            self.discovery_payload['category'] = category

    def command(self, payload):
        self.callback()


class LightEntity(Entity):
    def __init__(self, unique_id, name, device, command_topic, state_topic,
                 callback, brightness_scale=None, icon=None):
        super().__init__(unique_id, name, device, "light")
        self.state_topic = state_topic
        self.state = None
        self.command_topic = command_topic
        self.callback = callback
        self.discovery_payload.update({
            "schema": "json",
            "command_topic": self.command_topic,
            "state_topic": self.state_topic,
            "brightness": brightness_scale is not None,
        })
        if brightness_scale is not None:
            self.discovery_payload.update({
                "brightness_scale": brightness_scale,
            })
        if icon:
            self.discovery_payload.update({
                "icon": icon,
            })

    def set_state(self, state):
        prev_state = self.state
        self.state = json.dumps(state)
        if hasattr(self, "_hass") and self.state != prev_state:
            self._hass.update(self)

    def command(self, payload):
        self.callback(json.loads(payload))


class HomeAssistant:
    def __init__(self, config):
        self._config = config
        self.mqttc = mqtt.Client(client_id=config["mqtt_client_id"])
        if "mqtt_username" in config and "mqtt_password" in config:
            self.mqttc.username_pw_set(
                config["mqtt_username"], config["mqtt_password"])
        self.connected = False
        self.discovery_prefix = config["discovery_prefix"]
        self.tasks = []
        self.entities = []
        self.command_patterns = []
        self.command_topics = {}
        self.tasks = []
        self.idle_tasks = []
        self.pending_state_messages = []
        self.mqttc.connect_async(
            config["mqtt_hostname"], port=config["mqtt_port"])
        self.mqttc.on_connect = self.on_connect
        self.mqttc.on_message = self.on_message
        self.mqttc.message_callback_add(
            f"{self.discovery_prefix}/status", self.on_hass_status)

    def register_entity(self, entity):
        # Do we want to keep a dict of unique IDs to ensure there are
        # no collisions?
        self.entities.append(entity)
        entity._hass = self
        if hasattr(entity, "command_topic"):
            self.command_topics[entity.command_topic] = entity
        self.update(entity, send_discovery=True)

    def unregister_entity(self, entity):
        # We can't reliably do this — Home Assistant may be offline at
        # the time. Keep a null entry around so we can send a null
        # discovery payload when we or hass reconnect?
        pass

    def register_command_pattern(self, pattern):
        self.command_patterns.append(pattern)
        if self.connected:
            self.mqttc.subscribe([(pattern, 0)])

    def register_task(self, task):
        self.tasks.append(task)

    def register_idle_task(self, task):
        self.idle_tasks.append(task)

    def will_set(self, topic, payload):
        self.mqttc.will_set(topic, payload)

    def on_connect(self, client, userdata, flags, rc):
        log.debug(f"on_connect {rc=}")
        if rc == 5:
            log.fatal("mqtt: Not authorised")
            sys.exit(1)

        # XXX deal with all other known rc values here!

        if rc == 0:
            log.debug("Connected")
            client.subscribe(
                [(f"{self.discovery_prefix}/status", 0)]
                + [(p, 0) for p in self.command_patterns])
            self.update_all()

    def on_message(self, client, userdata, msg):
        entity = self.command_topics.get(msg.topic)
        if entity:
            entity.command(msg.payload)

    def on_hass_status(self, client, userdata, msg):
        if msg.payload == b"online":
            # Home Assistant has just come online, we need to resend all
            # config and state
            self.update_all()

    def update(self, entity, send_discovery=False):
        if self.connected:
            if send_discovery:
                self.mqttc.publish(
                    f"{self.discovery_prefix}/{entity.component}/"
                    f"{entity.unique_id}/config",
                    json.dumps(entity.discovery_payload))
            if hasattr(entity, "state_topic"):
                if entity.state is not None:
                    if send_discovery:
                        # hass doesn't appear to like to receive state updates
                        # immediately after discovery messages, if the
                        # discovery message caused it to register anything.
                        #
                        # To work around this, we save state changes to a list
                        # and send them after a delay
                        self.pending_state_messages.append(
                            (time.time() + 1.0, entity.state_topic,
                             entity.state))
                    else:
                        self.mqttc.publish(entity.state_topic, entity.state)

    def update_all(self):
        # Send discovery message and state for all entities
        for e in self.entities:
            self.update(e, send_discovery=True)

    def run(self):
        while True:
            while not self.connected:
                try:
                    self.mqttc.reconnect()
                    self.connected = True
                except ConnectionRefusedError:
                    time.sleep(1)

            if self.connected:
                now = time.time()
                current_tasks = [(t.deadline(), t) for t in self.tasks]
                current_tasks = sorted(
                    (t for t in current_tasks if t[0] is not None),
                    key=lambda x: x[0])
                if current_tasks:
                    timeout = max(current_tasks[0][0] - now, 0.0)
                else:
                    timeout = 60.0
                if self.pending_state_messages:
                    if now >= self.pending_state_messages[0][0]:
                        m = self.pending_state_messages.pop(0)
                        self.mqttc.publish(*m[1:])
                    else:
                        timeout = min(self.pending_state_messages[0][0] - now,
                                      timeout)
                # If timeout is greater than zero then we are idle
                if timeout > 0.0:
                    for t in self.idle_tasks:
                        t()
                rc = self.mqttc.loop(timeout=timeout)
                if rc == mqtt.MQTT_ERR_CONN_LOST:
                    self.connected = False
                if current_tasks and time.time() >= current_tasks[0][0]:
                    current_tasks[0][1].background()
