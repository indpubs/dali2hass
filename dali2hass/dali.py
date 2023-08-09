import logging
import time
from .hass import (
    Device,
    SensorEntity,
    BinarySensorEntity,
    ButtonEntity,
    SceneEntity,
    LightEntity,
)
from .drivers import available_drivers
from dali.address import Broadcast, Short, GearGroup
from dali.gear import emergency
from dali.gear.general import (
    QueryControlGearPresent,
    QueryActualLevel,
    QueryLampFailure,
    QuerySceneLevel,
    DAPC,
    Off,
    RecallMinLevel,
    RecallMaxLevel,
    QueryPhysicalMinimum,
    QueryMinLevel,
    QueryMaxLevel,
    GoToScene,
)
from dali.sequences import QueryDeviceTypes, QueryGroups
from dali.exceptions import DALISequenceError

log = logging.getLogger(__name__)


class Bridge:
    def __init__(self, config, hass, dry_run=False, max_address=63):
        self.log = log
        self.hass = hass
        self.dry_run = dry_run
        self.bus_id = config["bus_id"]
        self.bus_name = config["bus_name"]
        self.prefix = config["mqtt_prefix"]
        self.poll_interval = config["poll_interval"]
        self.gear_config = config["gear"]
        self.group_mode = config["groups"]
        self.groups_supported = self.group_mode != "off"
        driver = available_drivers.get(config["bus"]["driver"])
        if not driver:
            raise Exception("Unknown driver")
        self.bus = driver(config["bus"])

        self.has_emergency_units = False

        # Command topics will follow this pattern:
        self.hass.register_command_pattern(
            f"{self.prefix}/{self.bus_id}/+/command")

        self.bridge_device = Device(
            identifiers=[self.bus_id],
            name=self.bus_name,
            model="DALI bridge",
            manufacturer="dali2hass",
        )
        self.bridge_status = SensorEntity(
            self.make_uid("status"), "DALI bridge status",
            self.bridge_device, self.make_state_topic("status"),
            category="diagnostic")
        self.bridge_status.set_state("Initialising")
        self.hass.register_entity(self.bridge_status)
        self.hass.will_set(self.bridge_status.state_topic, "Stopped")

        # Control buttons
        self.hass.register_entity(ButtonEntity(
            self.make_uid("rescan"), "Rescan DALI devices",
            self.bridge_device,
            self.make_command_topic("rescan"), self.rescan_cmd,
            device_class="restart", category="config"))

        # Scenes
        self.scene_entities = {}

        # Groups
        self.groups = {}

        # Lights
        self.lights = [Light(x, self) for x in range(max_address + 1)]

        for light in self.lights:
            self.hass.register_task(light)

        self.update_status("Starting")
        self.init_complete = False
        self.hass.register_idle_task(self.idle)

    def update_status(self, message=None):
        if message:
            self.bridge_status.set_state(message)
        elif self.dry_run:
            self.bridge_status.set_state("Test mode")
        else:
            self.bridge_status.set_state("Running")

    def make_uid(self, uid):
        return f"{self.bus_id}_{uid}"

    def make_state_topic(self, uid):
        return f"{self.prefix}/{self.bus_id}/{uid}/state"

    def make_command_topic(self, uid):
        return f"{self.prefix}/{self.bus_id}/{uid}/command"

    def add_scene(self, scene):
        e = self.scene_entities.get(scene)
        if e:
            return
        # Add an entity for this scene
        self.scene_entities[scene] = SceneEntity(
            self.make_uid(f"scene_{scene}"), f"Scene {scene}",
            self.make_command_topic(f"scene_{scene}"),
            lambda: self.scene_cmd(scene))
        self.hass.register_entity(self.scene_entities[scene])

    def add_to_group(self, group, light):
        if group not in self.groups:
            self.groups[group] = Group(group, self)
        self.groups[group].add_member(light)
        return self.groups[group]

    def add_emergency_unit(self):
        if self.has_emergency_units:
            return
        self.has_emergency_units = True
        self.hass.register_entity(ButtonEntity(
            self.make_uid("inhibit"),
            "Inhibit emergency lighting for 15 minutes",
            self.bridge_device,
            self.make_command_topic("inhibit"), self.inhibit_cmd,
            category="config"))
        self.hass.register_entity(ButtonEntity(
            self.make_uid("reset_inhibit"), "Re-enable emergency lighting",
            self.bridge_device,
            self.make_command_topic("reset_inhibit"), self.reset_inhibit_cmd,
            category="config"))

    def scene_cmd(self, scene):
        self.log.debug("Go to scene %d", scene)
        if not self.dry_run:
            with self.bus as b:
                b.send(GoToScene(Broadcast(), scene))
        for light in self.lights:
            light.notify_scene(scene)

    def rescan_cmd(self):
        self.log.debug("Rescanning")
        for light in self.lights:
            light.scanned = False

    def inhibit_cmd(self):
        self.log.debug("Inhibit emergency lighting")
        if not self.dry_run:
            with self.bus as b:
                b.send(emergency.Inhibit(Broadcast()))

    def reset_inhibit_cmd(self):
        self.log.debug("Reset inhibit emergency lighting")
        if not self.dry_run:
            with self.bus as b:
                b.send(emergency.ReLightResetInhibit(Broadcast()))

    def idle(self):
        if self.init_complete:
            return
        self.init_complete = True
        if self.groups_supported:
            for group in self.groups.values():
                group.announce()
        self.update_status()


class Group:
    def __init__(self, address, bridge):
        self.log = log.getChild(f"Group({address})")
        self.log.debug("created")
        self.address = GearGroup(address)
        self.number = address
        self.bridge = bridge
        self.members = set()
        self.uid = f"group_{address}"
        # Home Assistant will add the device name to the entity name
        # automatically
        self.name = f"Group {address}"
        # Group has a minimum and maximum level — the lowest minimum
        # and highest maximum of all its members. It doesn't have a
        # physical minimum.
        #
        # Group supports brightness if any of its members support
        # brightness.
        #
        # Group brightness is determined by group mode:
        #  - min: the lowest brightness of any member
        #  - average: the average brightness of all members
        #  - max: the maximum brightness of any member

        # We defer creating an entity until we know about all our members
        # and whether they support brightness
        self.light_entity = None
        self.supports_brightness = False
        self.min_level = 254
        self.max_level = 1

    def announce(self):
        self.light_entity = LightEntity(
            self.bridge.make_uid(self.uid),
            self.name,
            self.bridge.bridge_device,
            self.bridge.make_command_topic(self.uid),
            self.bridge.make_state_topic(self.uid),
            self.command,
            brightness_scale=self.max_level
            if self.supports_brightness else None,
            icon="mdi:lightbulb-group")
        self.bridge.hass.register_entity(self.light_entity)

    def add_member(self, light):
        self.log.debug("added light at address %s", light.number)
        self.members.add(light)
        if light.supports_brightness:
            self.supports_brightness = True
        self.min_level = min(self.min_level, light.min_level)
        self.max_level = max(self.max_level, light.max_level)

    def command(self, sd):
        self.log.debug("group command %s", sd)


class Light:
    def __init__(self, address, bridge):
        self.log = log.getChild(f"Light({address})")
        self.address = Short(address)
        self.number = address
        self.bridge = bridge
        self.config = self.bridge.gear_config.get(str(address), {})
        self.scanned = False
        self.is_light = False
        self.last_update = time.time()
        self.scenes = {}
        self.groups = []
        self.physical_minimum = None
        self.min_level = None
        self.max_level = None
        self.level = None
        self.previous_active_level = None
        self.current_level = None
        self.lamp_failure = False
        self.supports_brightness = True
        self.uid = f"light_{address}"
        self.name = f"{self.bridge.bus_name} Light {address}"
        self.device = None
        self.light_entity = None
        self.lamp_failure_entity = None
        self.physical_minimum_entity = None

    def deadline(self):
        # return the time after which we want to be called back, or
        # None if no callback is needed
        if not self.scanned:
            return 0.0  # immediate
        if self.is_light:
            return self.last_update + self.bridge.poll_interval

    def background(self):
        # Called when the event loop is idle and the deadline has passed
        if not self.scanned:
            self.scan()
            return
        if not self.is_light:
            return
        self.log.debug("updating state in background...")
        with self.bridge.bus as b:
            new_level = b.send(QueryActualLevel(self.address)).value
            if new_level == "MASK":
                # The lamp may be in preheat or may have failed
                self.lamp_failure = b.send(
                    QueryLampFailure(self.address)).value
            elif isinstance(new_level, str):
                # Missing response or framing error
                self.log.debug("%s response to QueryActualLevel", new_level)
                return
            else:
                if new_level == 0 and self.current_level != 0:
                    self.previous_active_level = self.current_level
                self.current_level = new_level
        self.last_update = time.time()
        self.update_state()

    def scan(self):
        self.log.debug("scanning...")
        self.scanned = True
        self.supports_brightness = True
        with self.bridge.bus as b:
            present = b.send(QueryControlGearPresent(self.address)).value
            if not present:
                self.log.debug("no control gear present")
                return
            try:
                dts = b.send(QueryDeviceTypes(self.address))
            except DALISequenceError:
                # The device did not respond
                self.log.debug("could not read device type")
                return
            if 1 in dts:
                # It's an emergency unit.
                self.log.debug("is emergency unit")
                self.bridge.add_emergency_unit()
                features = b.send(
                    emergency.QueryEmergencyFeatures(self.address))
                if not features.switched_maintained_control_gear:
                    self.log.debug("not switched maintained gear, skipping")
                    return
            if 7 in dts:
                # This is a relay — on/off output only
                self.log.debug("is a relay, no brightness support")
                self.supports_brightness = False
            # XXX deal with the device failing to respond here...
            self.physical_minimum = \
                b.send(QueryPhysicalMinimum(self.address)).value
            if self.physical_minimum == 254:
                # Supported levels are 254 or 0 only
                self.log.debug("physical min is 254, no brightness support")
                self.supports_brightness = False
            # The config can override the supports_brightness flag:
            if "brightness" in self.config:
                self.log.debug("brightness support override in config")
                self.supports_brightness = self.config["brightness"]
            self.min_level = b.send(QueryMinLevel(self.address)).value
            self.max_level = b.send(QueryMaxLevel(self.address)).value
            self.current_level = b.send(QueryActualLevel(self.address)).value
            if self.current_level == "MASK":
                self.lamp_failure = b.send(
                    QueryLampFailure(self.address)).value
                self.current_level = 0
            self.previous_active_level = self.current_level or self.max_level
            self.scenes = {}
            for scene in range(16):
                sl = b.send(QuerySceneLevel(self.address, scene)).value
                if sl == "MASK":
                    continue
                elif isinstance(sl, str):
                    # missing response or framing error
                    return
                else:
                    self.scenes[scene] = sl
                    self.bridge.add_scene(scene)

            if self.bridge.groups_supported:
                self.groups = [self.bridge.add_to_group(g, self)
                               for g in b.send(QueryGroups(self.address))]

            if not self.device:
                self.device = Device(
                    identifiers=[self.bridge.make_uid(self.uid)],
                    name=self.name,
                    model=f"DALI light @ {self.number}",
                    manufacturer="dali2hass",
                    via_device=self.bridge.bus_id,
                )
            if not self.light_entity:
                self.light_entity = LightEntity(
                    self.bridge.make_uid(self.uid),
                    None,  # Entity will be given the Device Name by hass
                    self.device,
                    self.bridge.make_command_topic(self.uid),
                    self.bridge.make_state_topic(self.uid),
                    self.command,
                    brightness_scale=self.max_level
                    if self.supports_brightness else None)
                self.bridge.hass.register_entity(self.light_entity)
            if not self.lamp_failure_entity:
                self.lamp_failure_entity = BinarySensorEntity(
                    self.bridge.make_uid(f"{self.uid}_failure"),
                    "Lamp status",
                    self.device,
                    self.bridge.make_state_topic(f"{self.uid}_failure"),
                    device_class="problem", category="diagnostic")
                self.bridge.hass.register_entity(self.lamp_failure_entity)
            if not self.physical_minimum_entity:
                self.physical_minimum_entity = SensorEntity(
                    self.bridge.make_uid(f"{self.uid}_physical_minimum"),
                    "Physical minimum level",
                    self.device,
                    self.bridge.make_state_topic(
                        f"{self.uid}_physical_minimum"),
                    category="diagnostic")
                self.bridge.hass.register_entity(self.physical_minimum_entity)
            self.physical_minimum_entity.set_state(self.physical_minimum)
            self.is_light = True
            self.update_state()

    def update_state(self):
        sd = {
            "state": "ON" if self.current_level > 0 else "OFF",
        }
        if self.supports_brightness and self.current_level > 0:
            sd["brightness"] = self.current_level
        if self.light_entity:
            self.light_entity.set_state(sd)
        if self.lamp_failure_entity:
            self.lamp_failure_entity.set_state(self.lamp_failure)

    def send_cmd(self, command):
        if self.bridge.dry_run:
            self.log.debug("dry_run mode; not sending %s", command)
        else:
            self.log.debug("sending %s", command)
            with self.bridge.bus as b:
                b.send(command)

    def command(self, sd):
        log.debug("%s: command %s", self.address, sd)
        new_state = sd.get("state", "OFF")
        transition = sd.get("transition")
        if new_state == "OFF":
            target_level = 0
        else:
            target_level = sd.get("brightness", self.previous_active_level)
        if target_level == 0 and self.current_level > 0:
            self.previous_active_level = self.current_level
        if transition:
            self.log.debug(
                "transition=%s; using DAPC to set level", transition)
            self.send_cmd(DAPC(self.address, target_level))
            self.current_level = target_level
        else:
            self.log.debug("transition=%s; aiming for instant change",
                           transition)
            if target_level == 0:
                self.log.debug("target is zero; using Off")
                self.send_cmd(Off(self.address))
                self.current_level = 0
            elif target_level <= self.min_level:
                self.log.debug("target is below or at minimum level; "
                               "using RecallMinLevel")
                self.send_cmd(RecallMinLevel(self.address))
                self.current_level = self.min_level
            elif target_level >= self.max_level:
                self.log.debug("target is at or above maximum level; "
                               "using RecallMaxLevel")
                self.send_cmd(RecallMaxLevel(self.address))
                self.current_level = self.max_level
            else:
                self.log.debug("unable to perform instant change, using DAPC")
                self.send_cmd(DAPC(self.address, target_level))
                self.current_level = target_level
        self.last_update = time.time()
        self.update_state()

    def notify_scene(self, scene):
        if scene in self.scenes:
            # A scene change has been broadcast; this light will be
            # transitioning to the saved level
            new_level = self.scenes[scene]
            if new_level == 0 and self.current_level != 0:
                self.previous_active_level = self.current_level
            self.current_level = new_level
            self.last_update = time.time()
            self.update_state()

    def notify_group_action(self, new_level):
        # A command has been sent to a group of which this light is a
        # member; the command will result in us transitioning to the
        # specified new level
        if new_level == 0 and self.current_level != 0:
            self.previous_active_level = self.current_level
        self.current_level = new_level
        self.last_update = time.time()
        self.update_state()
