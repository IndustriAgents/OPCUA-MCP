# Alarms & Conditions mock OPC UA server

A small `node-opcua` server used by the end-to-end test suite to exercise
`list_active_alarms` and `acknowledge_alarm` on both MCP servers. Listens on
**:4842** (`opc.tcp://localhost:4842/UA/Alarms`) unless `ALARM_MOCK_PORT` says
otherwise — the test fixture always sets it to a free port of its own.

## Why a third mock

The main mock (`packages/mock-server`, python-opcua) raises plain events, and
that is as far as python-opcua's server goes:

- it has **no condition model**, so there is no `ConditionRefresh` to ask for
  retained alarms with, and no `Acknowledge` method to call on one;
- which makes it exactly the right server to assert that `list_active_alarms`
  fails *legibly* against something without Alarms & Conditions — the common
  case in the field.

This server covers the other half. node-opcua implements OPC UA Part 9 properly,
so the two alarm tools are tested against a real condition instance rather than
against something shaped like our own idea of one.

## Address space

| Node | Node ID | Notes |
|---|---|---|
| `Plant/Temperature` | `ns=1;i=1001` | `Double`, **writable**, starts at 100 |
| `Plant/HighTemperatureAlarm` | `ns=1;i=1002` | `ExclusiveLimitAlarmType`, high limit **80**, high-high limit 120 |

`Temperature` is writable so a test owns the alarm's state: write above the limit
for a fresh, unacknowledged alarm; write below it to clear one. It starts above
the limit, so there is an alarm to find without writing anything first.

The event hierarchy matters as much as the alarm: `Temperature` is an event
source of `Plant`, which is a notifier of the `Server` object. Without that
chain, a subscription on the Server object — the default notifier, and where
every OPC UA client looks first — would see none of these events.

`AccessHistoryDataCapability` is set to false and nothing is historized: this
mock is about alarms, and the history tools have their own.

## Running

```bash
npm install
npm start          # or: node server.mjs
```

By hand, against either MCP server:

```bash
OPCUA_SERVER_URL=opc.tcp://localhost:4842/UA/Alarms node ../server-node/build/index.js
```

Then `list_active_alarms`, `acknowledge_alarm` with the `event_id` it reported,
and `list_active_alarms` again to see `acked` flip. Acknowledging does not clear
the alarm — write `20` to `ns=1;i=1001` for that.
