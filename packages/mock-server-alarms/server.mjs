/**
 * Alarms & Conditions mock OPC UA server for the end-to-end test suite.
 *
 * The bundled Python mock (`packages/mock-server`) raises plain events, which is
 * as far as python-opcua's server goes: it has no condition model, so no
 * ConditionRefresh to ask for retained alarms with and no Acknowledge method to
 * call on one. That mock covers `subscribe_events` / `read_events`.
 *
 * This server covers the other half. node-opcua implements Part 9 properly, so
 * `list_active_alarms` and `acknowledge_alarm` are exercised against a real
 * condition instance rather than against something shaped like our own idea of
 * one — which is the only way those two tools can be said to work at all.
 *
 * `Temperature` is writable and drives an ExclusiveLimitAlarm above HIGH_LIMIT,
 * so a test owns the alarm's state: write above the limit for a fresh,
 * unacknowledged alarm; write below it to clear one. It starts above the limit,
 * so there is an alarm to find without writing anything first.
 */
import { DataType, OPCUAServer, Variant, coerceNodeId } from "node-opcua";

const PORT = Number(process.env.ALARM_MOCK_PORT ?? 4842);
const RESOURCE_PATH = "/UA/Alarms";

const HIGH_LIMIT = 80;
const HIGH_HIGH_LIMIT = 120;
const INITIAL_TEMPERATURE = 100;

const server = new OPCUAServer({
  port: PORT,
  resourcePath: RESOURCE_PATH,
  buildInfo: { productName: "OPC UA Alarm Mock" },
});

await server.initialize();

const addressSpace = server.engine.addressSpace;
addressSpace.installAlarmsAndConditionsService();

const namespace = addressSpace.getOwnNamespace();

// The event hierarchy the client walks: the alarm's source is the Temperature
// variable, which is an event source of Plant, which notifies the Server object.
// Without that chain a subscription on the Server object — the default notifier,
// and where every OPC UA client looks first — sees none of these events.
const plant = namespace.addObject({
  organizedBy: addressSpace.rootFolder.objects,
  browseName: "Plant",
  notifierOf: addressSpace.rootFolder.objects.server,
});

const temperature = namespace.addVariable({
  componentOf: plant,
  eventSourceOf: plant,
  browseName: "Temperature",
  dataType: "Double",
  minimumSamplingInterval: 100,
  accessLevel: "CurrentRead | CurrentWrite",
  userAccessLevel: "CurrentRead | CurrentWrite",
  value: { dataType: DataType.Double, value: INITIAL_TEMPERATURE },
});

const alarm = namespace.instantiateExclusiveLimitAlarm(
  "ExclusiveLimitAlarmType",
  {
    componentOf: plant,
    browseName: "HighTemperatureAlarm",
    conditionName: "HighTemperatureAlarm",
    conditionSource: temperature,
    inputNode: temperature,
    // ConfirmedState/Confirm make the acknowledge→confirm handshake real, and
    // ShelvingState makes the three shelve methods exist — without them a server
    // is still conformant and `act_on_alarm` has nothing to call, which is
    // exactly the case its refusals are worded for. Both are optional in Part 9,
    // so a mock that omitted them would leave half of #119 untestable.
    optionals: ["ConfirmedState", "Confirm", "ShelvingState"],
    highLimit: HIGH_LIMIT,
    highHighLimit: HIGH_HIGH_LIMIT,
  },
);
alarm.setEnabledState(true);

// Re-assert the initial value once the alarm is listening, so the server starts
// with the alarm already active rather than only after the first write.
temperature.setValueFromSource(
  new Variant({ dataType: DataType.Double, value: INITIAL_TEMPERATURE }),
);

// Advertise historical access the way the other mocks do, so a client connecting
// here is not surprised by a different tool list. Nothing is historized.
const historyCapability = addressSpace.findNode(coerceNodeId("ns=0;i=11193"));
if (historyCapability) {
  historyCapability.setValueFromSource({
    dataType: DataType.Boolean,
    value: false,
  });
}

await server.start();

// The test fixture waits for this line before connecting. Keep the prefix stable.
console.log(
  `READY endpoint=opc.tcp://localhost:${PORT}${RESOURCE_PATH} ` +
    `temperatureNodeId=${temperature.nodeId.toString()} ` +
    `alarmNodeId=${alarm.nodeId.toString()} ` +
    `highLimit=${HIGH_LIMIT}`,
);
