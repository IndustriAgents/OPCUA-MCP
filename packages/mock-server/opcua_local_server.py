import argparse
import copy
import logging
import random
import time
from datetime import timedelta

from opcua import Server, ua
from opcua.common.node import Node
from opcua.server.address_space import AttributeService


def answer_writes_to_unknown_nodes():
    """Make a write to a node the server does not have an answer, not a hang-up.

    python-opcua checks the AccessLevel bits of every node a non-admin session
    writes to — which is every client here, since the endpoints are anonymous —
    and reads them straight off what ``get_attribute_value`` returns. For a node
    id the address space does not have, that is an empty ``DataValue`` carrying
    BadNodeIdUnknown and a null Variant, so the bit test raises ``TypeError:
    unsupported operand type(s) for &: 'NoneType' and 'int'`` out of the request
    handler. The server then never answers the WriteRequest and drops the
    connection; the client waits out its own transaction timeout (15s in
    node-opcua) and every other node in the same batch is lost with it (#64).

    A conformant server answers per item: BadNodeIdUnknown for the node it does
    not have, Good for the ones it wrote. So screen the unknown ids out here and
    let the library write the rest. python-opcua is archived upstream in favour
    of asyncua, so this is patched at the mock rather than waiting for a release.
    """
    original_write = AttributeService.write

    def write(self, params, *args, **kwargs):
        known = [item for item in params.NodesToWrite if item.NodeId in self._aspace]
        if len(known) == len(params.NodesToWrite):
            return original_write(self, params, *args, **kwargs)

        statuses = iter(())
        if known:
            screened = copy.copy(params)
            screened.NodesToWrite = known
            statuses = iter(original_write(self, screened, *args, **kwargs))
        return [
            next(statuses)
            if item.NodeId in self._aspace
            else ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown)
            for item in params.NodesToWrite
        ]

    AttributeService.write = write


def _engineering_units(display: str, description: str) -> ua.EUInformation:
    """One ``EUInformation``, built field by field.

    python-opcua's generated structures take no keyword arguments, so this is
    what constructing one looks like. ``UnitId`` is the UNECE code the spec
    points at — 4408652 is "CEL", degree Celsius — and a real server publishes
    it, so a mock that omitted it would teach the wrong shape.
    """
    units = ua.EUInformation()
    units.NamespaceUri = "http://www.opcfoundation.org/UA/units/un/cefact"
    units.UnitId = 4408652
    units.DisplayName = ua.LocalizedText(display)
    units.Description = ua.LocalizedText(description)
    return units


def _range(low: float, high: float) -> ua.Range:
    """One ``Range``, the two-number structure EURange and InstrumentRange are."""
    value = ua.Range()
    value.Low = low
    value.High = high
    return value


class IndustrialControlSystem:
    """Mock Industrial Control System with sensors, actuators, and control methods."""

    def __init__(self, server: Server):
        self.server = server
        self.running = False

        # System state
        self.system_state = {
            # Sensor readings
            "temperature": 25.0,
            "pressure": 1013.25,
            "flow_rate": 150.0,
            "tank_level": 75.0,
            "vibration": 0.5,
            "ph_level": 7.2,
            "humidity": 45.0,
            "motor_speed": 1500.0,
            # Actuator states
            "pump_enabled": False,
            "valve_position": 50.0,
            "heater_power": 0.0,
            "fan_speed": 0.0,
            "conveyor_speed": 0.0,
            "alarm_active": False,
            # System status
            "system_mode": "MANUAL",  # MANUAL, AUTO, MAINTENANCE
            "emergency_stop": False,
            "production_rate": 0.0,
            "total_production": 0.0,
        }

        # Node references for efficient updates
        self.nodes = {}

        # Event emission (see `setup_events`). The generator is created once the
        # server is running; until then, transitions are simply not announced.
        self.event_generator = None
        self.alarm_reason = ""
        self._alarm_was_active = False

    def setup_address_space(self):
        """Setup the OPC UA address space with industrial control structure."""

        # Get the root object node
        objects = self.server.get_objects_node()

        # Create main industrial system folder
        industrial_system = objects.add_folder(2, "IndustrialControlSystem")

        # Create sensor folder and variables
        sensors_folder = industrial_system.add_folder(2, "Sensors")
        self._create_sensor_variables(sensors_folder)

        # Create actuator folder and variables
        actuators_folder = industrial_system.add_folder(2, "Actuators")
        self._create_actuator_variables(actuators_folder)

        # Create system status folder
        system_folder = industrial_system.add_folder(2, "SystemStatus")
        self._create_system_variables(system_folder)

        # Create methods folder and add control methods
        methods_folder = industrial_system.add_folder(2, "Methods")
        self._create_control_methods(methods_folder)

        # Create scratch folder: the only writable nodes the simulation leaves alone
        scratch_folder = industrial_system.add_folder(2, "Scratch")
        self._create_scratch_variables(scratch_folder)

        logging.info("Address space setup completed")

    def historize(self):
        accessHistoryDataCapability = self.server.get_node("ns=0;i=11193")
        accessHistoryDataCapability.set_value(True)

        objects = self.server.get_objects_node()
        industrial_system = objects.get_child("2:IndustrialControlSystem")
        for child in industrial_system.get_children():
            for variable in child.get_variables():
                logging.info(
                    f"historize {child.get_display_name().to_string()};"
                    f"{variable.get_display_name().to_string()}"
                )
                self.server.historize_node_data_change(
                    variable, period=timedelta(minutes=10), count=0
                )

        logging.info("historize completed")

    def setup_events(self):
        """Announce alarm transitions as OPC UA events.

        Emitted from the **Server** object (`ns=0;i=2253`) rather than from the
        plant folder, because python-opcua's server delivers an event only to
        monitored items on the node that emits it — it does not propagate one up
        the notifier hierarchy the way a spec-complete server does. A client
        subscribing to the Server object, which is where clients look first and
        what both MCP servers subscribe to by default, would otherwise never see
        these. `SourceNode`/`SourceName` still name the plant, so the event says
        what it is about.

        These are plain `BaseEventType` events, not conditions: python-opcua has
        no condition model, so there is nothing here to acknowledge and
        `ConditionRefresh` is not implemented. `packages/mock-server-alarms` is
        the mock that covers that half.
        """
        self.event_generator = self.server.get_event_generator()
        plant = self.server.get_objects_node().get_child("2:IndustrialControlSystem")
        self.event_generator.event.SourceNode = plant.nodeid
        self.event_generator.event.SourceName = "IndustrialControlSystem"
        logging.info("event generator ready")

    def _emit_alarm_transitions(self):
        """Fire an event when the alarm state changes, and only then."""
        if self.event_generator is None:
            return

        active = bool(self.system_state["alarm_active"])
        if active == self._alarm_was_active:
            return
        self._alarm_was_active = active

        if active:
            self.event_generator.event.Severity = ua.Variant(700, ua.VariantType.UInt16)
            message = f"Alarm active: {self.alarm_reason or 'unspecified condition'}"
        else:
            self.event_generator.event.Severity = ua.Variant(100, ua.VariantType.UInt16)
            message = "Alarm cleared"

        try:
            self.event_generator.trigger(message=message)
            logging.info(f"event: {message}")
        except Exception as e:
            logging.error(f"Error triggering alarm event: {e}")

    def _create_sensor_variables(self, parent_folder: Node):
        """Create sensor variables with proper data types and descriptions."""

        # Temperature sensor
        temp_node = parent_folder.add_variable(2, "Temperature", self.system_state["temperature"])
        temp_node.set_writable(False)
        self.nodes["temperature"] = temp_node

        # Pressure sensor
        pressure_node = parent_folder.add_variable(2, "Pressure", self.system_state["pressure"])
        pressure_node.set_writable(False)
        self.nodes["pressure"] = pressure_node

        # Flow rate sensor
        flow_node = parent_folder.add_variable(2, "FlowRate", self.system_state["flow_rate"])
        flow_node.set_writable(False)
        self.nodes["flow_rate"] = flow_node

        # Tank level sensor
        level_node = parent_folder.add_variable(2, "TankLevel", self.system_state["tank_level"])
        level_node.set_writable(False)
        self.nodes["tank_level"] = level_node

        # Vibration sensor
        vibration_node = parent_folder.add_variable(2, "Vibration", self.system_state["vibration"])
        vibration_node.set_writable(False)
        self.nodes["vibration"] = vibration_node

        # pH sensor
        ph_node = parent_folder.add_variable(2, "PhLevel", self.system_state["ph_level"])
        ph_node.set_writable(False)
        self.nodes["ph_level"] = ph_node

        # Humidity sensor
        humidity_node = parent_folder.add_variable(2, "Humidity", self.system_state["humidity"])
        humidity_node.set_writable(False)
        self.nodes["humidity"] = humidity_node

        # Motor speed sensor
        motor_speed_node = parent_folder.add_variable(
            2, "MotorSpeed", self.system_state["motor_speed"]
        )
        motor_speed_node.set_writable(False)
        self.nodes["motor_speed"] = motor_speed_node

    def _create_scratch_variables(self, parent_folder: Node):
        """Writable variables the simulation never touches.

        Every other writable node here is an *actuator*, and the simulation loop
        republishes each one from `system_state` once a second — which is
        realistic, and is why the README tells people their writes to those nodes
        are transient. It also means a test that writes a value and reads it back
        is racing a one-second timer: it passes locally, passes in CI, and then
        fails in a release verify. That is exactly what happened to
        `test_batch_write_keeps_valid_items_when_one_node_is_rejected`.

        So these exist purely to be written and read back. Nothing simulates
        them, nothing else reads them, and they are outside `system_state` so
        `_update_opcua_nodes` cannot reach them. A Double and a Boolean, because
        the write path converts by data type and those are the two that catch a
        conversion bug (a Boolean written as 1/0, a string not parsed to a
        number).
        """
        scratch_double = parent_folder.add_variable(2, "ScratchDouble", 0.0)
        scratch_double.set_writable(True)
        self.scratch_double = scratch_double

        scratch_bool = parent_folder.add_variable(2, "ScratchBoolean", False)
        scratch_bool.set_writable(True)
        self.scratch_bool = scratch_bool

        self._create_analog_scratch(parent_folder)

    def _create_analog_scratch(self, parent_folder: Node):
        """A scratch variable that says what its number *means*.

        Every other node here is a bare value, which is how most of an address
        space looks and is exactly the problem: an agent handed `51.75` cannot
        tell °C from PSI from %, and cannot tell a reading from a trip. OPC UA
        Part 8 §5.3 answers that with `AnalogItemType` — `EngineeringUnits`,
        `EURange` for what the value holds in normal operation, and
        `InstrumentRange` for what the device can physically return — and a real
        PLC or SCADA server publishes all three on an analogue tag.

        The node id is explicit rather than assigned in sequence. Every other
        node here takes whatever number it happens to get, which means adding one
        in the middle renumbers everything after it and silently breaks the map
        in `tests/e2e/test_mcp_e2e.py`. A node added later should not be able to
        do that.

        `EURange` is deliberately narrower than `InstrumentRange`: the write path
        enforces the first and reports the second, and if they were equal no test
        could tell which one it had enforced.
        """
        node = parent_folder.add_variable(
            ua.NodeId(90, 2), ua.QualifiedName("ScratchAnalog", 2), 50.0
        )
        node.set_writable(True)
        node.add_property(
            ua.NodeId(91, 2),
            ua.QualifiedName("EngineeringUnits", 0),
            _engineering_units("°C", "degree Celsius"),
        )
        node.add_property(ua.NodeId(92, 2), ua.QualifiedName("EURange", 0), _range(0.0, 150.0))
        node.add_property(
            ua.NodeId(93, 2), ua.QualifiedName("InstrumentRange", 0), _range(-50.0, 250.0)
        )
        # What makes it an AnalogItem rather than a Variable that happens to have
        # three properties. Nothing in this project reads the type definition
        # yet, but a mock that lies about what it is teaches the wrong lesson to
        # whatever reads it next.
        node.add_reference(ua.NodeId(ua.ObjectIds.AnalogItemType), ua.ObjectIds.HasTypeDefinition)
        self.scratch_analog = node

    def _create_actuator_variables(self, parent_folder: Node):
        """Create actuator variables that can be controlled."""

        # Pump control
        pump_node = parent_folder.add_variable(2, "PumpEnabled", self.system_state["pump_enabled"])
        pump_node.set_writable(True)
        self.nodes["pump_enabled"] = pump_node

        # Valve position control
        valve_node = parent_folder.add_variable(
            2, "ValvePosition", self.system_state["valve_position"]
        )
        valve_node.set_writable(True)
        self.nodes["valve_position"] = valve_node

        # Heater power control
        heater_node = parent_folder.add_variable(
            2, "HeaterPower", self.system_state["heater_power"]
        )
        heater_node.set_writable(True)
        self.nodes["heater_power"] = heater_node

        # Fan speed control
        fan_node = parent_folder.add_variable(2, "FanSpeed", self.system_state["fan_speed"])
        fan_node.set_writable(True)
        self.nodes["fan_speed"] = fan_node

        # Conveyor speed control
        conveyor_node = parent_folder.add_variable(
            2, "ConveyorSpeed", self.system_state["conveyor_speed"]
        )
        conveyor_node.set_writable(True)
        self.nodes["conveyor_speed"] = conveyor_node

        # Alarm control
        alarm_node = parent_folder.add_variable(2, "AlarmActive", self.system_state["alarm_active"])
        alarm_node.set_writable(True)
        self.nodes["alarm_active"] = alarm_node

    def _create_system_variables(self, parent_folder: Node):
        """Create system status variables."""

        # System mode
        mode_node = parent_folder.add_variable(2, "SystemMode", self.system_state["system_mode"])
        mode_node.set_writable(True)
        self.nodes["system_mode"] = mode_node

        # Emergency stop
        estop_node = parent_folder.add_variable(
            2, "EmergencyStop", self.system_state["emergency_stop"]
        )
        estop_node.set_writable(True)
        self.nodes["emergency_stop"] = estop_node

        # Production rate
        prod_rate_node = parent_folder.add_variable(
            2, "ProductionRate", self.system_state["production_rate"]
        )
        prod_rate_node.set_writable(False)
        self.nodes["production_rate"] = prod_rate_node

        # Total production
        total_prod_node = parent_folder.add_variable(
            2, "TotalProduction", self.system_state["total_production"]
        )
        total_prod_node.set_writable(False)
        self.nodes["total_production"] = total_prod_node

        # Command variables for control (alternative to methods)
        start_prod_cmd = parent_folder.add_variable(2, "StartProductionCommand", 0.0)
        start_prod_cmd.set_writable(True)
        self.nodes["start_production_command"] = start_prod_cmd

        stop_prod_cmd = parent_folder.add_variable(2, "StopProductionCommand", False)
        stop_prod_cmd.set_writable(True)
        self.nodes["stop_production_command"] = stop_prod_cmd

        emergency_cmd = parent_folder.add_variable(2, "EmergencyStopCommand", False)
        emergency_cmd.set_writable(True)
        self.nodes["emergency_stop_command"] = emergency_cmd

        reset_cmd = parent_folder.add_variable(2, "ResetSystemCommand", False)
        reset_cmd.set_writable(True)
        self.nodes["reset_system_command"] = reset_cmd

    def _create_control_methods(self, parent_folder: Node):
        """Create OPC UA methods for system control."""

        # Start production method (input: rate Double, output: Boolean)
        parent_folder.add_method(
            2,
            "StartProduction",
            self.start_production_callback,
            [ua.VariantType.Double],
            [ua.VariantType.Boolean],
        )

        # Stop production method (output: Boolean)
        parent_folder.add_method(
            2, "StopProduction", self.stop_production_callback, [], [ua.VariantType.Boolean]
        )

        # Emergency stop method (output: Boolean)
        parent_folder.add_method(
            2, "EmergencyStop", self.emergency_stop_callback, [], [ua.VariantType.Boolean]
        )

        # Reset system method (output: Boolean)
        parent_folder.add_method(
            2, "ResetSystem", self.reset_system_callback, [], [ua.VariantType.Boolean]
        )

        # Calibrate sensors method (input: sensor name String, output: Boolean)
        parent_folder.add_method(
            2,
            "CalibrateSensors",
            self.calibrate_sensors_callback,
            [ua.VariantType.String],
            [ua.VariantType.Boolean],
        )

    # Method callbacks.
    # OPC UA passes inputs as ua.Variant objects (use .Value) and expects
    # the callback to return a list of ua.Variant output values.
    def start_production_callback(self, parent, *args):
        """Start production with specified rate."""
        rate = float(args[0].Value) if args else 10.0
        logging.info(f"Starting production with rate: {rate}")
        self.system_state["production_rate"] = rate
        self.system_state["system_mode"] = "AUTO"
        self.system_state["pump_enabled"] = True
        self.system_state["conveyor_speed"] = min(rate * 2, 100.0)  # Scale speed with rate
        return [ua.Variant(True, ua.VariantType.Boolean)]

    def stop_production_callback(self, parent, *args):
        """Stop production."""
        logging.info("Stopping production")
        self.system_state["production_rate"] = 0.0
        self.system_state["system_mode"] = "MANUAL"
        self.system_state["pump_enabled"] = False
        self.system_state["conveyor_speed"] = 0.0
        return [ua.Variant(True, ua.VariantType.Boolean)]

    def emergency_stop_callback(self, parent, *args):
        """Trigger emergency stop."""
        logging.warning("EMERGENCY STOP TRIGGERED!")
        self.alarm_reason = "emergency stop"
        self.system_state["emergency_stop"] = True
        self.system_state["system_mode"] = "MAINTENANCE"
        self.system_state["production_rate"] = 0.0
        self.system_state["pump_enabled"] = False
        self.system_state["conveyor_speed"] = 0.0
        self.system_state["heater_power"] = 0.0
        self.system_state["fan_speed"] = 0.0
        self.system_state["alarm_active"] = True
        return [ua.Variant(True, ua.VariantType.Boolean)]

    def reset_system_callback(self, parent, *args):
        """Reset system to initial state."""
        logging.info("Resetting system")
        self.system_state["emergency_stop"] = False
        self.system_state["system_mode"] = "MANUAL"
        self.system_state["alarm_active"] = False
        self.system_state["total_production"] = 0.0
        return [ua.Variant(True, ua.VariantType.Boolean)]

    def calibrate_sensors_callback(self, parent, *args):
        """Calibrate specified sensor."""
        sensor_name = str(args[0].Value) if args else "unknown"
        logging.info(f"Calibrating sensor: {sensor_name}")
        # Simulate calibration by adding small random offset
        if sensor_name in self.system_state:
            # Add some calibration effect
            pass
        return [ua.Variant(True, ua.VariantType.Boolean)]

    def simulate_process(self):
        """Simulate industrial process behavior."""
        while self.running:
            try:
                # Process command variables first
                self._process_commands()

                # Update sensor readings based on system state
                self._update_sensors()

                # Update actuator effects
                self._process_actuator_effects()

                # Update production metrics
                self._update_production()

                # Update all OPC UA nodes
                self._update_opcua_nodes()

                # Announce any change in the alarm state
                self._emit_alarm_transitions()

                time.sleep(1.0)  # Update every second

            except Exception as e:
                logging.error(f"Error in process simulation: {e}")
                time.sleep(1.0)

    def _process_commands(self):
        """Process command variables and execute corresponding actions."""
        try:
            # Check start production command
            start_cmd = self.nodes["start_production_command"].get_value()
            if start_cmd > 0:
                rate = float(start_cmd)
                logging.info(f"Starting production with rate: {rate}")
                self.system_state["production_rate"] = rate
                self.system_state["system_mode"] = "AUTO"
                self.system_state["pump_enabled"] = True
                self.system_state["conveyor_speed"] = min(rate * 2, 100.0)
                # Reset command
                self.nodes["start_production_command"].set_value(0.0)

            # Check stop production command
            stop_cmd = self.nodes["stop_production_command"].get_value()
            if stop_cmd:
                logging.info("Stopping production")
                self.system_state["production_rate"] = 0.0
                self.system_state["system_mode"] = "MANUAL"
                self.system_state["pump_enabled"] = False
                self.system_state["conveyor_speed"] = 0.0
                # Reset command
                self.nodes["stop_production_command"].set_value(False)

            # Check emergency stop command
            emergency_cmd = self.nodes["emergency_stop_command"].get_value()
            if emergency_cmd:
                logging.warning("EMERGENCY STOP TRIGGERED!")
                self.alarm_reason = "emergency stop"
                self.system_state["emergency_stop"] = True
                self.system_state["system_mode"] = "MAINTENANCE"
                self.system_state["production_rate"] = 0.0
                self.system_state["pump_enabled"] = False
                self.system_state["conveyor_speed"] = 0.0
                self.system_state["heater_power"] = 0.0
                self.system_state["fan_speed"] = 0.0
                self.system_state["alarm_active"] = True
                # Reset command
                self.nodes["emergency_stop_command"].set_value(False)

            # Check reset system command
            reset_cmd = self.nodes["reset_system_command"].get_value()
            if reset_cmd:
                logging.info("Resetting system")
                self.system_state["emergency_stop"] = False
                self.system_state["system_mode"] = "MANUAL"
                self.system_state["alarm_active"] = False
                self.system_state["total_production"] = 0.0
                # Reset command
                self.nodes["reset_system_command"].set_value(False)

        except Exception as e:
            logging.error(f"Error processing commands: {e}")

    def _update_sensors(self):
        """Update sensor readings with realistic variations."""

        # Temperature influenced by heater
        base_temp = 25.0 + (self.system_state["heater_power"] * 0.5)
        self.system_state["temperature"] = base_temp + random.uniform(-2.0, 2.0)

        # Pressure influenced by pump and valve
        base_pressure = 1013.25
        if self.system_state["pump_enabled"]:
            base_pressure += 50.0
        base_pressure -= (self.system_state["valve_position"] - 50.0) * 0.5
        self.system_state["pressure"] = base_pressure + random.uniform(-5.0, 5.0)

        # Flow rate influenced by pump and valve
        if self.system_state["pump_enabled"]:
            base_flow = 150.0 * (self.system_state["valve_position"] / 100.0)
        else:
            base_flow = 0.0
        self.system_state["flow_rate"] = max(0, base_flow + random.uniform(-10.0, 10.0))

        # Tank level influenced by flow rate and production
        level_change = (
            self.system_state["flow_rate"] - self.system_state["production_rate"]
        ) * 0.01
        self.system_state["tank_level"] = max(
            0, min(100, self.system_state["tank_level"] + level_change + random.uniform(-0.5, 0.5))
        )

        # Vibration influenced by motor and conveyor speed
        base_vibration = (self.system_state["motor_speed"] / 1500.0) * 0.3
        base_vibration += (self.system_state["conveyor_speed"] / 100.0) * 0.2
        self.system_state["vibration"] = base_vibration + random.uniform(-0.1, 0.1)

        # pH level with slow drift
        self.system_state["ph_level"] += random.uniform(-0.05, 0.05)
        self.system_state["ph_level"] = max(6.0, min(8.0, self.system_state["ph_level"]))

        # Humidity influenced by temperature
        base_humidity = 45.0 - (self.system_state["temperature"] - 25.0) * 2.0
        self.system_state["humidity"] = max(20, min(80, base_humidity + random.uniform(-3.0, 3.0)))

        # Motor speed influenced by production rate
        if self.system_state["production_rate"] > 0:
            self.system_state["motor_speed"] = (
                1500.0 + (self.system_state["production_rate"] * 10.0) + random.uniform(-50.0, 50.0)
            )
        else:
            self.system_state["motor_speed"] = random.uniform(-10.0, 10.0)

    def _process_actuator_effects(self):
        """Process effects of actuator changes."""

        # Check for alarm conditions
        reasons = []
        if self.system_state["temperature"] > 80.0:
            reasons.append(f"temperature {self.system_state['temperature']:.1f}")
        if self.system_state["pressure"] > 1200.0:
            reasons.append(f"pressure {self.system_state['pressure']:.1f}")
        if self.system_state["tank_level"] < 10.0:
            reasons.append(f"tank level {self.system_state['tank_level']:.1f}")
        if self.system_state["vibration"] > 2.0:
            reasons.append(f"vibration {self.system_state['vibration']:.2f}")
        if reasons:
            self.alarm_reason = ", ".join(reasons)
            self.system_state["alarm_active"] = True

        # Auto-safety: stop system if emergency conditions
        if self.system_state["temperature"] > 100.0 or self.system_state["pressure"] > 1300.0:
            self.system_state["emergency_stop"] = True
            self.system_state["system_mode"] = "MAINTENANCE"

    def _update_production(self):
        """Update production metrics."""
        if (
            self.system_state["system_mode"] == "AUTO"
            and not self.system_state["emergency_stop"]
            and self.system_state["production_rate"] > 0
        ):
            # Add to total production (rate is per hour, we update per second)
            self.system_state["total_production"] += self.system_state["production_rate"] / 3600.0

    def _update_opcua_nodes(self):
        """Update all OPC UA node values."""
        for key, node in self.nodes.items():
            if key in self.system_state:
                try:
                    node.set_value(self.system_state[key])
                except Exception as e:
                    logging.error(f"Error updating node {key}: {e}")

    def start_simulation(self):
        """Start the process simulation."""
        self.running = True

    def stop_simulation(self):
        """Stop the process simulation."""
        self.running = False


DEFAULT_ENDPOINT = "opc.tcp://0.0.0.0:4840/freeopcua/server/"


def main():
    """Main function to run the OPC UA server."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=(
            "OPC UA endpoint to listen on (default: %(default)s). The test suite "
            "passes an ephemeral port so parallel checkouts never share a server."
        ),
    )
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    answer_writes_to_unknown_nodes()

    # Create and configure the server
    server = Server()

    # Set server endpoint
    server.set_endpoint(args.endpoint)

    # Set server name and namespace
    server.set_server_name("Industrial Control System OPC UA Server")

    # Setup security policy (optional)
    server.set_security_policy(
        [
            ua.SecurityPolicyType.NoSecurity,
            ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
            ua.SecurityPolicyType.Basic256Sha256_Sign,
        ]
    )

    # Create the industrial control system
    industrial_system = IndustrialControlSystem(server)

    try:
        # Setup the address space
        industrial_system.setup_address_space()

        # Start the server
        server.start()
        industrial_system.historize()
        industrial_system.setup_events()
        logging.info(f"OPC UA Server started at {args.endpoint}")
        logging.info("Server is running and ready for connections")

        # Start process simulation in a separate thread
        industrial_system.start_simulation()

        import threading

        simulation_thread = threading.Thread(target=industrial_system.simulate_process)
        simulation_thread.daemon = True
        simulation_thread.start()

        try:
            # Keep the server running
            logging.info("Server running. Press Ctrl+C to stop...")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logging.info("Shutting down server...")
            industrial_system.stop_simulation()

    except Exception as e:
        logging.error(f"Server error: {e}")
        raise
    finally:
        try:
            if server is not None:
                server.stop()
        except Exception as e:
            logging.error(f"Error stopping server: {e}")


if __name__ == "__main__":
    main()
