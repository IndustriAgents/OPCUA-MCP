# OPC UA MCP Server

A Model Context Protocol (MCP) server that provides seamless integration with OPC UA servers. This server enables AI assistants and other MCP clients to interact with industrial automation systems through standardized OPC UA communication protocols.

## Overview

This MCP server acts as a bridge between AI assistants and OPC UA servers, allowing for:
- Reading sensor data and system variables
- Writing control values to actuators and systems
- Browsing OPC UA node hierarchies
- Calling OPC UA methods for system operations
- Batch operations for multiple nodes
- Subscribing to data changes, so a node can be watched rather than polled
- Reporting connection and server health, and reconnecting by itself when the
  OPC UA server is restarted

## Tools

See the central per-tool reference in **[docs/examples.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md)**; the shared tool surface is defined in **[contract/tools.json](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/tools.json)**.

## Resources

One resource, `opcua://subscriptions`: the active data-change subscriptions and the values each has buffered, as JSON. It is the same set of records `list_subscriptions` returns, re-readable without spending a tool call. See [Subscriptions](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md#data-change-subscriptions) for the shape and the worked example.

## Features

### Key Capabilities

- **Automatic Connection Management**: Handles OPC UA client lifecycle with proper connection setup and teardown
- **Type-Safe Operations**: Automatic type conversion based on existing node data types
- **Error Handling**: Comprehensive error reporting for debugging and monitoring
- **Async Support**: Built on the `mcp` SDK's `MCPServer` for efficient asynchronous operations
- **Data-Change Subscriptions**: Monitored items buffered as they arrive, torn down with the session
- **Configurable**: Environment-based endpoint, security policy and credentials

## Installation

### Prerequisites

- Python 3.10 or higher
- Access to an OPC UA server (local or remote)
- UV package manager (recommended) or pip

### From PyPI (recommended)

```bash
uvx opcua-mcp-server            # run without installing
uv tool install opcua-mcp-server   # or install permanently
pip install opcua-mcp-server       # or with pip
```

Point it at your OPC UA endpoint:

```bash
export OPCUA_SERVER_URL="opc.tcp://localhost:4840"
```

### Registering with Claude Desktop

Rather than editing `claude_desktop_config.json` by hand, let the server write it:

```bash
opcua-mcp-server --install claude-desktop --url opc.tcp://192.168.0.10:4840
```

It merges into the existing config, backs the old one up, and records absolute
paths — Claude Desktop is launched from the GUI and does not inherit a login
shell's `PATH`, so a bare `"command": "uvx"` often works in a terminal and fails
in the app. Add `--dry-run` to see the result first, `--force` to replace an
existing `opcua` entry.

There is also a **downloadable `.mcpb` bundle** for Claude Desktop and
**single-file executables** that need no Python at all — see
[docs/install.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/install.md).

In an MCP client:

```json
{
  "mcpServers": {
    "opcua": {
      "command": "uvx",
      "args": ["opcua-mcp-server"],
      "env": { "OPCUA_SERVER_URL": "opc.tcp://localhost:4840" }
    }
  }
}
```

### From source (development)

1. **Install dependencies for the whole workspace (run from the repo root):**
   ```bash
   uv sync --all-packages
   ```

2. **Configure the OPC UA server URL:**
   ```bash
   export OPCUA_SERVER_URL="opc.tcp://localhost:4840"
   ```

## Configuration

The server is configured entirely through environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `OPCUA_SERVER_URL` | `opc.tcp://localhost:4840` | OPC UA endpoint to connect to |
| `OPCUA_SECURITY_POLICY` | `None` | `None`, `Basic128Rsa15`, `Basic256` or `Basic256Sha256` (the AES suites are Node-only) |
| `OPCUA_SECURITY_MODE` | `SignAndEncrypt` once a policy is set, otherwise `None` | `None`, `Sign` or `SignAndEncrypt` |
| `OPCUA_CLIENT_CERT` | — | Client certificate (PEM/DER). Required for any policy other than `None` |
| `OPCUA_CLIENT_KEY` | — | Private key for `OPCUA_CLIENT_CERT` |
| `OPCUA_APPLICATION_URI` | the `subjectAltName` URI of `OPCUA_CLIENT_CERT` | Application URI announced to the server. Set it only for a certificate that carries no URI of its own |
| `OPCUA_USERNAME` | — | Username identity; the session is anonymous when unset |
| `OPCUA_PASSWORD` | — | Password for `OPCUA_USERNAME` |

Names are case-insensitive, and a policy on its own implies `SignAndEncrypt`.
An unusable combination — a mode without a policy, a policy without a
certificate, a username without a password — is refused at startup with a
message naming the variable. With no security configured the connection is
unencrypted and unauthenticated, and the server says so on stderr; see
[SECURITY.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/SECURITY.md).
Making a client certificate and getting it trusted:
[docs/certificates.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/certificates.md).

On the **Python runtime**, certificate and key files are parsed as PEM only when
they are named `*.pem` and as DER otherwise (a `python-opcua` rule), so a PEM key
called `client.key` fails to load — name it `client_key.pem`. The Node runtime
sniffs the contents and accepts either name.

```bash
export OPCUA_SERVER_URL="opc.tcp://plc.example.internal:4840"
export OPCUA_SECURITY_POLICY="Basic256Sha256"     # implies SignAndEncrypt
export OPCUA_CLIENT_CERT="/etc/opcua/client.pem"
export OPCUA_CLIENT_KEY="/etc/opcua/client_key.pem"
export OPCUA_USERNAME="mcp-operator"
export OPCUA_PASSWORD="…"
```

### Staying connected

The connection is re-established by itself: a dropped or refused session is
retried with exponential backoff, and the read and write paths rebuild a dead
session rather than failing until the process is restarted. `python-opcua` has no
reconnection of its own, so this server owns the whole of it — including building
a fresh client per attempt, because a restarted server may present a new
certificate. Tune it with `OPCUA_RECONNECT_INITIAL_DELAY_MS` (default `1000`),
`OPCUA_RECONNECT_MAX_DELAY_MS` (`8000`), `OPCUA_RECONNECT_MAX_RETRY` (`3`; `-1`
retries forever) and `OPCUA_SESSION_TIMEOUT_MS` (`60000`, which also sets the
keep-alive period).

`get_server_status` reports whether the connection is up and what the OPC UA
server says about itself; it is the one tool that answers while the connection is
down, and calling it is also what brings a dropped one back. See
[Staying connected](https://github.com/IndustriAgents/OPCUA-MCP#staying-connected).

## Usage

### Running the Server

After `uv sync --all-packages` from the repo root:
```bash
uv run --no-sync opcua-mcp-server
```

Or run it directly against this package from anywhere in the repo:
```bash
uv --directory packages/server-python run opcua-mcp-server
```

### Integration with MCP Clients

Add to your MCP client configuration (e.g., `config.json`):

```json
{
  "mcpServers": {
    "opcua-mcp": {
      "command": "/path/to/uv",
      "args": [
        "--directory",
        "/path/to/packages/server-python",
        "run",
        "opcua-mcp-server"
      ],
      "env": {
        "OPCUA_SERVER_URL": "opc.tcp://localhost:4840"
      }
    }
  }
}
```

## Example Usage in Conversation

Once configured, you can ask Claude to perform real-world OPC UA operations:

### Reading Sensor Values
- "What is the current temperature reading from the reactor vessel?"
- "Check the pressure level in Tank A"
- "Read all temperature sensors in the cooling system"
- "Get the current flow rate on production line B"

### Controlling Equipment
- "Turn on the conveyor motor"
- "Set the mixer speed to 80 RPM"
- "Open valve V-101 to 75% position"
- "Start the circulation pump"
- "Set the heating element to 150°C"

### Production Operations
- "Start production on line 1 with rate 100 units/hour"
- "Stop all motors in emergency mode"
- "Initialize the batch reactor with recipe A"
- "Begin the cleaning cycle for Tank B"

### System Monitoring
- "Show me all alarm states in the system"
- "Read all sensor values from the water treatment plant"
- "Check the status of all pumps and motors"
- "Get the energy consumption readings"
- "What variables are available on this OPC UA server?"
- "Show me all sensors and their current values"

### Real Example Scenarios
```
User: "What's the temperature in reactor R-101?"
Assistant: I'll read the temperature sensor for reactor R-101.
Tool call: read_opcua_nodes with node_ids ["ns=2;i=11"]
Result: "Reactor R-101 temperature: 87.3°C"

User: "Turn on the main conveyor motor"
Assistant: I'll start the main conveyor motor for you.
Tool call: write_opcua_nodes with nodes [{node_id: "ns=2;i=12", value: true}]
Result: "Main conveyor motor started successfully"

User: "Set the pump speed to 65%"
Assistant: I'll adjust the pump speed to 65%.
Tool call: write_opcua_nodes with nodes [{node_id: "ns=2;i=13", value: 65}]
Result: "Pump speed set to 65%"

User: "What variables are available on this OPC UA server?"
Assistant: I'll retrieve all available variables from the OPC UA server.
Tool call: browse_opcua_nodes
Result: Found 5 variables:
- Temperature (ns=2;i=2): 25.3°C - Temperature sensor
- Pressure (ns=2;i=3): 5.0 bar - Pressure sensor  
- MotorSpeed (ns=2;i=4): 1500 RPM - Motor speed
- MotorState (ns=2;i=5): True - Motor ON/OFF state
- ValvePosition (ns=2;i=6): False - Valve OPEN/CLOSED position
```

## API Reference

See the central per-tool reference in **[docs/examples.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md)** for full tool signatures, parameters, and return formats. The shared tool surface is defined in **[contract/tools.json](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/tools.json)**.