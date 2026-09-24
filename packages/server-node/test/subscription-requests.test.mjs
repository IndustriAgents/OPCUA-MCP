// What this runtime asks an OPC UA server for when it subscribes, and what an
// event subscription does when the session under it is replaced (#157).
//
// `tests/unit/test_subscriptions.py` and `tests/unit/test_events.py` assert the
// same of the Python runtime, against the same contract values. Both used to be
// written out here and left to python-opcua's defaults there, so the two asked a
// server for subscriptions that outlived their client by a minute and by hours.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { DataType, StatusCodes, Variant } from "node-opcua-client";

import { CONTRACT } from "../build/contract.js";
import { EventSubscriptions } from "../build/events.js";
import { SubscriptionManager } from "../build/subscriptions.js";

/** A stand-in session recording every CreateSubscription request it is sent. */
function recordingSession({ refuse = false } = {}) {
  const session = {
    requests: [],
    listeners: [],
    async createSubscription2(request) {
      session.requests.push(request);
      return {
        async terminate() {},
        async monitor() {
          if (refuse) throw new Error("BadNodeIdUnknown");
          return {
            statusCode: StatusCodes.Good,
            on(_event, listener) {
              session.listeners.push(listener);
            },
          };
        },
      };
    },
  };
  return session;
}

/** One event's select-clause values: nulls, which is a plain event with no severity. */
function anEvent() {
  return CONTRACT.events.fields.map(() => new Variant({ dataType: DataType.Null }));
}

describe("CreateSubscription requests", () => {
  it("a data-change subscription asks for what the contract names", async () => {
    const session = recordingSession();
    await new SubscriptionManager().subscribe(session, "ns=2;i=3", { publishingInterval: 200 });
    const request = CONTRACT.subscriptions.request;
    assert.deepEqual(session.requests, [
      {
        requestedPublishingInterval: 200,
        requestedMaxKeepAliveCount: request.maxKeepAliveCount,
        requestedLifetimeCount: request.lifetimeCount,
        maxNotificationsPerPublish: request.maxNotificationsPerPublish,
        publishingEnabled: true,
        priority: request.priority,
      },
    ]);
  });

  it("an event subscription asks for what the contract names", async () => {
    const session = recordingSession();
    await new EventSubscriptions().subscribe(session, "ns=0;i=2253", 0, 10);
    const request = CONTRACT.events.subscriptionRequest;
    assert.deepEqual(session.requests, [
      {
        requestedPublishingInterval: request.publishingIntervalMs,
        requestedLifetimeCount: request.lifetimeCount,
        requestedMaxKeepAliveCount: request.maxKeepAliveCount,
        maxNotificationsPerPublish: request.maxNotificationsPerPublish,
        publishingEnabled: true,
        priority: request.priority,
      },
    ]);
  });
});

describe("event subscriptions across a new session", () => {
  it("are re-created, keep their buffer, and report the gap once", async () => {
    const subscriptions = new EventSubscriptions();
    const old = recordingSession();
    await subscriptions.subscribe(old, "ns=0;i=2253", 0, 10);
    old.listeners[0](anEvent());

    const replacement = recordingSession();
    await subscriptions.reattach(replacement);

    assert.equal(replacement.requests.length, 1, "no subscription was created on the new session");
    const drained = subscriptions.drain(replacement, "ns=0;i=2253", 10);
    assert.equal(drained.records.length, 1, "the buffer did not survive the new session");
    assert.equal(drained.resubscribed, true);
    assert.equal(subscriptions.drain(replacement, "ns=0;i=2253", 10).resubscribed, false);

    replacement.listeners[0](anEvent());
    assert.equal(subscriptions.drain(replacement, "ns=0;i=2253", 10).records.length, 1);
  });

  it("are dropped when the new session refuses them", async () => {
    const subscriptions = new EventSubscriptions();
    await subscriptions.subscribe(recordingSession(), "ns=0;i=2253", 0, 10);
    const refusing = recordingSession({ refuse: true });
    await subscriptions.reattach(refusing);
    assert.equal(subscriptions.drain(refusing, "ns=0;i=2253", 10), null);
  });
});
