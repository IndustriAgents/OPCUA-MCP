// What each alarm action calls, on what, with what (issue #119).
//
// `acknowledge_alarm` implemented the first half of OPC UA Part 9 §5.5's
// acknowledge→confirm handshake and nothing else, so an agent could say "I have
// seen this" and then had no way to say "I have dealt with it", to leave a note,
// or to do what an operator actually does with a chattering nuisance alarm.
//
// This is where the *request* is pinned, because a fake session can be asked
// exactly what it was handed and a real server cannot. The end-to-end tests in
// `tests/e2e/test_events_e2e.py` cover the parts that need a real condition — and
// in the shelving case they can only assert routing, because node-opcua implements
// that state machine partly and unreliably. So this file carries the load.
//
// `tests/unit/test_alarm_actions.py` is the Python half and asserts the same
// calls.
//
// Two things here are easy to get wrong and do not fail cleanly:
//
// **Which object.** The acknowledge family are methods of the condition's own
// type. The three shelving ones are methods of ShelvedStateMachineType and hang
// off the condition's `ShelvingState` component. Resolved against the wrong
// object, a server finds a *different* method of the right name's neighbour and
// answers `BadArgumentsMissing` or `BadTooManyArguments` — which is what this did
// before the routing was fixed.
//
// **Which node id.** The first draft used `ns=0;i=9211/9213/9215`, which are
// `AlarmConditionType_ShelvingState_*` and are *not* in the order the names
// suggest. The right fallbacks are ShelvedStateMachineType's own: Unshelve 2947,
// OneShotShelve 2948, TimedShelve 2949.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DataType, StatusCodes } from "node-opcua-client";

import { CONTRACT } from "../build/contract.js";
import { ACTIONS, acknowledgeAlarm, alarmAction } from "../build/events.js";

const CONDITION_ID = "ns=2;i=100";
const EVENT_ID = "YWJjMTIz"; // base64 of "abc123"
const SHELVING = CONTRACT.events.shelvingStateBrowseName;

/** A session whose address space is a map of parent id -> children by browse name. */
function fakeSession(tree) {
  const calls = [];
  return {
    calls,
    async browse({ nodeId }) {
      const children = tree[nodeId.toString()] ?? {};
      return {
        references: Object.entries(children).map(([name, id]) => ({
          browseName: { name },
          nodeId: { toString: () => id },
        })),
      };
    },
    async call(request) {
      calls.push(request);
      return { statusCode: StatusCodes.Good };
    },
  };
}

describe("the action table", () => {
  it("fully specifies every action it declares", () => {
    // A half-declared action would resolve to something, silently.
    for (const [name, spec] of Object.entries(ACTIONS)) {
      assert.deepEqual(
        Object.keys(spec).sort(),
        ["browseName", "description", "methodNodeId", "on", "takes"],
        name
      );
      assert.ok(["condition", "shelvingState"].includes(spec.on), name);
      assert.ok(["eventIdAndComment", "duration", "nothing"].includes(spec.takes), name);
      assert.match(spec.methodNodeId, /^ns=0;i=/, name);
    }
  });

  it("hangs the shelving actions off the shelving state and the rest off the condition", () => {
    // The distinction that produced BadArgumentsMissing when it was missed.
    const on = (which) =>
      Object.entries(ACTIONS)
        .filter(([, spec]) => spec.on === which)
        .map(([name]) => name)
        .sort();
    assert.deepEqual(on("shelvingState"), ["shelve", "shelveFor", "unshelve"]);
    assert.deepEqual(on("condition"), ["acknowledge", "comment", "confirm"]);
  });

  // ShelvedStateMachineType's own methods, which are *not* numbered in the order
  // the names suggest — the first draft had all three wrong.
  for (const [action, nodeId] of [
    ["unshelve", "ns=0;i=2947"],
    ["shelve", "ns=0;i=2948"],
    ["shelveFor", "ns=0;i=2949"],
    ["acknowledge", "ns=0;i=9111"],
    ["confirm", "ns=0;i=9113"],
    ["comment", "ns=0;i=9029"],
  ]) {
    it(`falls back to the spec's ${action} method id`, () => {
      assert.equal(ACTIONS[action].methodNodeId, nodeId);
    });
  }
});

describe("what actually goes on the wire", () => {
  it("passes the event id and the comment for the acknowledge family", async () => {
    const session = fakeSession({ [CONDITION_ID]: { Confirm: "ns=2;i=110" } });

    await alarmAction(session, CONDITION_ID, EVENT_ID, "confirm", "dealt with");

    assert.equal(session.calls.length, 1);
    const [request] = session.calls;
    assert.equal(request.objectId, CONDITION_ID, "called on the condition itself");
    assert.equal(request.methodId, "ns=2;i=110", "the condition's own method");
    assert.equal(request.inputArguments.length, 2);
    // The event id is decoded from base64 to a ByteString.
    assert.equal(request.inputArguments[0].dataType, DataType.ByteString);
    assert.equal(request.inputArguments[0].value.toString("utf8"), "abc123");
    assert.equal(request.inputArguments[1].dataType, DataType.LocalizedText);
    assert.equal(request.inputArguments[1].value.text, "dealt with");
  });

  it("makes acknowledge_alarm the same call by another name", async () => {
    // One implementation, two entry points — the property #119 had to preserve.
    const session = fakeSession({ [CONDITION_ID]: { Acknowledge: "ns=2;i=111" } });

    await acknowledgeAlarm(session, CONDITION_ID, EVENT_ID, "seen");

    const [request] = session.calls;
    assert.equal(request.methodId, "ns=2;i=111");
    assert.equal(request.inputArguments[0].value.toString("utf8"), "abc123");
  });

  it("sends one Double of milliseconds for a timed shelve", async () => {
    // Duration is a Double in OPC UA, not a struct. Sending it any other way is
    // what answered BadTooManyArguments.
    const session = fakeSession({
      [CONDITION_ID]: { [SHELVING]: "ns=2;i=200" },
      "ns=2;i=200": { TimedShelve: "ns=2;i=201" },
    });

    await alarmAction(session, CONDITION_ID, EVENT_ID, "shelveFor", "", 30000);

    const [request] = session.calls;
    assert.equal(request.objectId, "ns=2;i=200", "called on the ShelvingState, not the condition");
    assert.equal(request.methodId, "ns=2;i=201");
    assert.equal(request.inputArguments.length, 1, "TimedShelve takes exactly one argument");
    assert.equal(request.inputArguments[0].dataType, DataType.Double);
    assert.equal(request.inputArguments[0].value, 30000);
  });

  for (const action of ["shelve", "unshelve"]) {
    it(`sends nothing for ${action}`, async () => {
      const browseName = ACTIONS[action].browseName;
      const session = fakeSession({
        [CONDITION_ID]: { [SHELVING]: "ns=2;i=200" },
        "ns=2;i=200": { [browseName]: "ns=2;i=201" },
      });

      await alarmAction(session, CONDITION_ID, EVENT_ID, action);

      const [request] = session.calls;
      assert.equal(request.objectId, "ns=2;i=200");
      assert.equal(request.methodId, "ns=2;i=201");
      assert.deepEqual(request.inputArguments, [], `${browseName} takes no arguments`);
    });
  }

  it("refuses a condition with no ShelvingState, and says why", async () => {
    // ShelvingState is optional in Part 9, so a server without it is conformant.
    // Falling back to the type node would be worse than useless: shelving is
    // per-instance state.
    const session = fakeSession({ [CONDITION_ID]: { Acknowledge: "ns=2;i=111" } });

    await assert.rejects(
      () => alarmAction(session, CONDITION_ID, EVENT_ID, "shelve"),
      /has no ShelvingState/
    );
    assert.deepEqual(session.calls, [], "nothing was sent");
  });

  it("falls back to the type's method when the condition hides its own", async () => {
    // Part 9 allows a server not to expose condition instances at all. The type's
    // own method is then called with the condition as the object, which is what
    // acknowledge_alarm has always done and what the rest now do too.
    const session = fakeSession({ [CONDITION_ID]: {} });

    await alarmAction(session, CONDITION_ID, EVENT_ID, "comment", "note");

    const [request] = session.calls;
    assert.equal(request.objectId, CONDITION_ID);
    assert.equal(request.methodId, ACTIONS.comment.methodNodeId);
  });
});
