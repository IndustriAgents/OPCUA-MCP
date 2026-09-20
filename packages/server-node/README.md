# OPC UA MCP Server (Node)

A Node / TypeScript Model Context Protocol (MCP) server for OPC UA operations, runnable with `npx`. This server provides a set of tools to interact with OPC UA servers, including reading/writing variables, browsing nodes, calling methods, and performing batch operations.

## Features

- **Read OPC UA Nodes**: Read values from individual or multiple OPC UA nodes
- **Write OPC UA Nodes**: Write values to individual or multiple OPC UA nodes
- **Browse Node Children**: Explore the OPC UA address space by browsing node children
- **Call OPC UA Methods**: Execute methods on OPC UA objects with parameters
- **Batch Operations**: Perform multiple read/write operations in single requests
- **Get All Variables**: Discover all available variables in the OPC UA server address space
- **Automatic Type Conversion**: Intelligent conversion of values based on node data types
- **Connection Management**: Automatic connection handling with graceful disconnection
- **Read History OPC UA Node**: Read the historical values of a specific OPC UA node (if supported by the server)
- **Read Aggregate OPC UA Node**: Calculate the historical aggregates (if supported by the server)
- **Data-Change Subscriptions**: Watch a node and read back the values it has delivered, instead of polling it

## Installation & Usage

### Using npx (Recommended)

You can run the server directly using npx without installing it globally:

```bash
npx opcua-mcp-server
```

### Global Installation

```bash
npm install -g opcua-mcp-server
opcua-mcp-server
```

### Registering with Claude Desktop

Rather than editing `claude_desktop_config.json` by hand, let the server write it:

```bash
opcua-mcp-server --install claude-desktop --url opc.tcp://192.168.0.10:4840
```

It merges into the existing config, backs the old one up, and records absolute
paths — Claude Desktop is launched from the GUI and does not inherit a login
shell's `PATH`, so a bare `"command": "npx"` often works in a terminal and fails
in the app. Add `--dry-run` to see the result first, `--force` to replace an
existing `opcua` entry.

There is also a **downloadable `.mcpb` bundle** for Claude Desktop and
**single-file executables** that need no Node at all — see
[docs/install.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/install.md).

### Local Development

```bash
git clone <repository>
cd packages/server-node
npm install
npm run build
npm start
```

## Configuration

The server is configured entirely through environment variables:

| Variable                | Default                                                 | Meaning                                                                                                |
| ----------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `OPCUA_SERVER_URL`      | `opc.tcp://localhost:4840`                              | OPC UA endpoint to connect to                                                                          |
| `OPCUA_SECURITY_POLICY` | `None`                                                  | `None`, `Basic128Rsa15`, `Basic256`, `Basic256Sha256`, `Aes128_Sha256_RsaOaep`, `Aes256_Sha256_RsaPss` |
| `OPCUA_SECURITY_MODE`   | `SignAndEncrypt` once a policy is set, otherwise `None` | `None`, `Sign` or `SignAndEncrypt`                                                                     |
| `OPCUA_CLIENT_CERT`     | —                                                       | Client certificate (PEM/DER). Required for any policy other than `None`                                |
| `OPCUA_CLIENT_KEY`      | —                                                       | Private key for `OPCUA_CLIENT_CERT`                                                                    |
| `OPCUA_APPLICATION_URI` | the `subjectAltName` URI of `OPCUA_CLIENT_CERT`         | Application URI announced to the server. Set it only for a certificate that carries no URI of its own  |
| `OPCUA_USERNAME`        | —                                                       | Username identity; the session is anonymous when unset                                                 |
| `OPCUA_PASSWORD`        | —                                                       | Password for `OPCUA_USERNAME`                                                                          |

Names are case-insensitive, and a policy on its own implies `SignAndEncrypt`.
An unusable combination — a mode without a policy, a policy without a
certificate, a username without a password — is refused at startup with a
message naming the variable. With no security configured the connection is
unencrypted and unauthenticated, and the server says so on stderr; see
[SECURITY.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/SECURITY.md).
Making a client certificate and getting it trusted:
[docs/certificates.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/certificates.md).

Examples:

```bash
# unsecured, e.g. against the bundled mock
OPCUA_SERVER_URL=opc.tcp://192.168.1.100:4840 npx opcua-mcp-server

# encrypted and authenticated
OPCUA_SERVER_URL=opc.tcp://plc.example.internal:4840 \
OPCUA_SECURITY_POLICY=Basic256Sha256 \
OPCUA_CLIENT_CERT=/etc/opcua/client.pem \
OPCUA_CLIENT_KEY=/etc/opcua/client_key.pem \
OPCUA_USERNAME=mcp-operator OPCUA_PASSWORD=… \
  npx opcua-mcp-server
```

### Staying connected

The connection is re-established by itself: a dropped or refused session is
retried with exponential backoff, and the read and write paths rebuild a dead
session rather than failing until the process is restarted. Tune it with
`OPCUA_RECONNECT_INITIAL_DELAY_MS` (default `1000`), `OPCUA_RECONNECT_MAX_DELAY_MS`
(`8000`), `OPCUA_RECONNECT_MAX_RETRY` (`3`; `-1` retries forever) and
`OPCUA_SESSION_TIMEOUT_MS` (`60000`, which also sets the keep-alive period).

`get_server_status` reports whether the connection is up and what the OPC UA
server says about itself; it is the one tool that answers while the connection is
down, and calling it is also what brings a dropped one back. See
[Staying connected](https://github.com/IndustriAgents/OPCUA-MCP#staying-connected).

## Tools

This server exposes the shared OPC UA MCP tool set. See the full per-tool reference (inputs, outputs, node-ID map) in **[docs/examples.md](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md)**. The tool surface is defined once in **[contract/tools.json](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/contract/tools.json)**, which this server builds its `tools/list` from.

## Resources

One resource, `opcua://subscriptions`: the active data-change subscriptions and the values each has buffered, as JSON. It is the same set of records `list_subscriptions` returns, re-readable without spending a tool call. See [Subscriptions](https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/examples.md#data-change-subscriptions) for the shape and the worked example.

## Integration with Cursor/Claude

This server can be integrated with Cursor IDE or Claude Desktop for OPC UA operations. Add the following to your MCP configuration:

### Cursor Configuration

Add to your Cursor settings:

```json
{
  "mcpServers": {
    "opcua-node": {
      "command": "npx",
      "args": ["opcua-mcp-server"],
      "env": {
        "OPCUA_SERVER_URL": "opc.tcp://localhost:4840"
      }
    }
  }
}
```

### Claude Desktop Configuration

Add to your Claude Desktop configuration file:

```json
{
  "mcpServers": {
    "opcua-node": {
      "command": "npx",
      "args": ["opcua-mcp-server"],
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
- "Discover all available variables in the OPC UA server"
- "Show me a complete inventory of all sensors and actuators"
- "List all process variables with their current values"

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

User: "Show me all available variables in the system"
Assistant: I'll get a complete list of all variables in the OPC UA server.
Tool call: browse_opcua_nodes
Result: "Found 15 variables:
- Name: Temperature_Sensor_01, NodeID: ns=2;i=101, Value: 87.3°C
- Name: Pressure_Sensor_01, NodeID: ns=2;i=102, Value: 2.5 bar
- Name: Flow_Rate_01, NodeID: ns=2;i=103, Value: 125.8 L/min
..."
```

## Security Considerations

- The connection defaults to no security (`SecurityPolicy.None`); set
  `OPCUA_SECURITY_POLICY` and credentials as shown under
  [Configuration](#configuration) for anything beyond local development
- The server certificate is taken from the endpoint description and is not
  pinned or checked against a trust list
- Ensure proper network security when connecting to industrial OPC UA servers
- Validate and sanitize all input parameters
- Scope the OPC UA account you connect with to what the assistant should be able
  to do — it can write nodes and call methods

## Error Handling

The server provides detailed error messages for:

- Connection failures
- Invalid node IDs
- Type conversion errors
- Method call failures
- Read/write operation errors

## Dependencies

- `@modelcontextprotocol/sdk`: MCP SDK for Node.js
- `node-opcua-client`: OPC UA client library for Node.js (the client half of `node-opcua`; the server half is not needed here and is deliberately not a dependency)
- `typescript`: TypeScript compiler

## Contributing

We welcome contributions to improve the OPC UA MCP Server!

**Repository**: [https://github.com/IndustriAgents/OPCUA-MCP](https://github.com/IndustriAgents/OPCUA-MCP)

To contribute:

1. Fork the repository at [https://github.com/IndustriAgents/OPCUA-MCP](https://github.com/IndustriAgents/OPCUA-MCP)
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Add tests if applicable
5. Commit your changes (`git commit -m 'Add some amazing feature'`)
6. Push to the branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

Please feel free to open issues for bug reports, feature requests, or questions.

## License

MIT License - see LICENSE file for details

## Support

For issues and questions:

- Open an issue on [GitHub](https://github.com/IndustriAgents/OPCUA-MCP/issues)
- Check the OPC UA server connectivity
- Verify node IDs are correct
- Ensure proper permissions for OPC UA operations
- Review server logs for detailed error information
