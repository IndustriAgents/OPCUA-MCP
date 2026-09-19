# OPC UA Industrial Control System Server

A comprehensive mockup OPC UA server for industrial control systems, featuring realistic sensor variables, actuator controls, and system management methods.

## Features

### 🔧 **Sensor Variables** (Read-Only)
- **Temperature** - Process temperature in Celsius (°C)
- **Pressure** - System pressure in hectopascals (hPa)
- **Flow Rate** - Fluid flow rate in liters per minute (L/min)
- **Tank Level** - Tank level percentage (0-100%)
- **Vibration** - Vibration level in mm/s
- **pH Level** - Chemical pH measurement (6.0-8.0)
- **Humidity** - Relative humidity percentage (20-80%)
- **Motor Speed** - Motor rotation speed in RPM

### ⚙️ **Actuator Variables** (Read/Write)
- **Pump Enabled** - Main pump on/off control (Boolean)
- **Valve Position** - Valve position percentage (0-100%)
- **Heater Power** - Heater power percentage (0-100%)
- **Fan Speed** - Fan speed percentage (0-100%)
- **Conveyor Speed** - Conveyor belt speed percentage (0-100%)
- **Alarm Active** - System alarm status (Boolean)

### 🧪 **Scratch Variables** (Read/Write, never simulated)
- **ScratchDouble** (`ns=2;i=41`) and **ScratchBoolean** (`ns=2;i=42`) — written
  and read back by tests. Every other writable node is an actuator the simulation
  republishes from its own state once a second, so writing one and reading it back
  races a timer.
- **ScratchAnalog** (`ns=2;i=90`) — the one node that says what its number
  *means*. An OPC UA `AnalogItemType` (Part 8 §5.3) publishing `EngineeringUnits`
  (°C), `EURange` (0 to 150) and `InstrumentRange` (-50 to 250). The two ranges
  are deliberately different, so a test can tell which one the MCP servers
  enforce on a write. Its node id is fixed rather than assigned in sequence, so
  adding a node above it cannot renumber it.

### 📊 **System Status** (Mixed)
- **System Mode** - Operation mode: MANUAL, AUTO, MAINTENANCE (Read/Write)
- **Emergency Stop** - Emergency stop status (Read/Write)
- **Production Rate** - Current production rate in units/hour (Read-Only)
- **Total Production** - Cumulative production count (Read-Only)

### 🎯 **OPC UA Methods**
1. **StartProduction(rate: Double) → Boolean**
   - Starts production with specified rate
   - Automatically enables pump and sets conveyor speed

2. **StopProduction() → Boolean**
   - Stops production process
   - Disables pump and stops conveyor

3. **EmergencyStop() → Boolean**
   - Triggers immediate system shutdown
   - Sets all actuators to safe state
   - Activates alarm

4. **ResetSystem() → Boolean**
   - Resets system to initial state
   - Clears emergency stop and alarms

5. **CalibrateSensors(sensorName: String) → Boolean**
   - Simulates sensor calibration process

### 🔔 **Events**
The server raises a plain `BaseEventType` event whenever its alarm state
*changes* — severity 700 for `Alarm active: <reason>`, 100 for `Alarm cleared` —
so `subscribe_events` and `read_events` have something real to collect. Trigger
one by writing `true` to `EmergencyStopCommand`, and clear it with
`ResetSystemCommand`.

Events are emitted from the **Server** object (`ns=0;i=2253`), because
python-opcua delivers an event only to monitored items on the node that emitted
it — it does not propagate one up the notifier hierarchy. `SourceNode` and
`SourceName` still name the plant, so the event says what it is about.

There are no **conditions** here: python-opcua has no condition model, so
`ConditionRefresh` is not implemented and there is nothing to acknowledge.
`packages/mock-server-alarms` is the mock for `list_active_alarms` and
`acknowledge_alarm`.

## Installation

1. Ensure you have Python 3.10+ installed
2. Install dependencies for the whole workspace (run from the repo root):
   ```bash
   uv sync --all-packages
   ```

## Usage

### Starting the Server
```bash
uv run opcua-mock-server
```

The server will start on `opc.tcp://0.0.0.0:4840/freeopcua/server/`. Pass
`--endpoint` to listen elsewhere:

```bash
uv run opcua-mock-server --endpoint opc.tcp://127.0.0.1:14840/freeopcua/server/
```

The e2e suite uses this to give every test session a mock on a port of its own.

### Connecting with OPC UA Clients

You can connect to the server using any OPC UA client:

#### Example with opcua-client (Python)
```python
from opcua import Client

client = Client("opc.tcp://localhost:4840/freeopcua/server/")
client.connect()

# Read temperature sensor
temp_node = client.get_node("ns=2;s=IndustrialControlSystem.Sensors.Temperature")
temperature = temp_node.get_value()
print(f"Temperature: {temperature}°C")

# Control pump
pump_node = client.get_node("ns=2;s=IndustrialControlSystem.Actuators.PumpEnabled")
pump_node.set_value(True)

# Call start production method
methods_node = client.get_node("ns=2;s=IndustrialControlSystem.Methods")
start_method = methods_node.get_child("StartProduction")
result = start_method.call_method(50.0)  # Start with 50 units/hour

client.disconnect()
```

## System Behavior

### Realistic Simulation
The server simulates realistic industrial process behavior:

- **Temperature** varies based on heater power settings
- **Pressure** responds to pump operation and valve positions
- **Flow Rate** depends on pump status and valve opening
- **Tank Level** changes based on flow rate vs. production rate
- **Vibration** correlates with motor and conveyor speeds

### Safety Features
- Automatic alarms for out-of-range conditions, announced as OPC UA events
- Emergency stop functionality
- Auto-shutdown for critical conditions (>100°C, >1300 hPa)

### Production Control
- Real-time production rate monitoring
- Cumulative production tracking
- Automatic equipment coordination

## Server Architecture

```
IndustrialControlSystem/
├── Sensors/
│   ├── Temperature
│   ├── Pressure
│   ├── FlowRate
│   ├── TankLevel
│   ├── Vibration
│   ├── PhLevel
│   ├── Humidity
│   └── MotorSpeed
├── Actuators/
│   ├── PumpEnabled
│   ├── ValvePosition
│   ├── HeaterPower
│   ├── FanSpeed
│   ├── ConveyorSpeed
│   └── AlarmActive
├── SystemStatus/
│   ├── SystemMode
│   ├── EmergencyStop
│   ├── ProductionRate
│   └── TotalProduction
└── Methods/
    ├── StartProduction
    ├── StopProduction
    ├── EmergencyStop
    ├── ResetSystem
    └── CalibrateSensors
```

## Security

The server supports multiple security policies:
- No Security (for testing)
- Basic256Sha256 with signing
- Basic256Sha256 with signing and encryption

## Development

### File Structure
- `opcua_local_server.py` - Main server implementation and entry point
- `examples/mock_server_client_demo.py` (repo root) - Example OPC UA client
- `pyproject.toml` - Project configuration and dependencies

### Patched library behaviour

The server keeps one monkey-patch over python-opcua,
`answer_writes_to_unknown_nodes()`, applied in `main()`. Writing to a node id the
address space does not have made the library raise out of its request handler, so
the mock answered the `WriteRequest` not at all and closed the connection: the
client waited out its own transaction timeout (15s in node-opcua) and lost every
other node in the same batch, including ones the mock had already written (#64).
The patch screens unknown ids out and answers them `BadNodeIdUnknown` alongside
the `Good` of the rest, which is what a conformant server does. python-opcua is
archived upstream in favour of asyncua, so this is fixed here rather than waited
out. `tests/e2e/test_mock_server_e2e.py` pins it — with a bare OPC UA client,
because both MCP servers read a node before writing it and so never send the
request that trips it.

### Extending the System
To add new sensors or actuators:

1. Add variables to `system_state` dictionary
2. Create nodes in respective `_create_*_variables` methods
3. Update simulation logic in `_update_sensors` method
4. Add any new methods in `_create_control_methods`
5. Announce anything worth an operator's attention from `_emit_alarm_transitions`

## Troubleshooting

### Common Issues

**Port Already in Use**
```
OSError: [Errno 48] Address already in use
```
Solution: Kill existing server process or change port in code

**Connection Refused**
- Check if server is running
- Verify firewall settings
- Ensure client is connecting to correct endpoint

### Logging
The server provides detailed logging. Check console output for:
- Server startup messages
- Connection events
- Method calls
- Error messages

## License

This project is for educational and demonstration purposes.

## Contributing

Feel free to submit issues and enhancement requests!
