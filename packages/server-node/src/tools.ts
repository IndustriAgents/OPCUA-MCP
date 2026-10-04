import { ProtocolFeatureHandlers } from "./protocol/feature-handlers.js";
import { NodeOpcuaRuntime } from "./adapters/opcua-runtime.js";
import {
  auditPermission,
  auditAfter,
  describeTargets,
  newCallId,
} from "./infrastructure/control-audit.js";
export { describeTargets, newCallId } from "./infrastructure/control-audit.js";
import { dispatchFeature } from "./protocol/dispatch.js";
import { statusResult, outputSchema } from "./protocol/results.js";
// The OPC UA tool implementations.
//
// Compose instance-owned native runtime and protocol/application handlers.
// See docs/feature-modules.md for the dependency boundaries.
import { invokeTool } from "./application/invocation.js";
import { executeTool, type ExecutionCall } from "./application/execution.js";

import { Resource, Tool } from "@modelcontextprotocol/sdk/types.js";

export { toNodeValueRecord } from "./adapters/opcua-read.js";

import { type CapabilityAnswers } from "./capabilities.js";

import { OpcuaConnection, isConnectionError } from "./connection.js";
import type { ToolName } from "./generated/contract-types.js";
import { CONTRACT, type ToolSpec } from "./contract.js";

import { AuditSink } from "./audit.js";
import { ToolFailure, UnexpectedToolFailure, describeError } from "./errors.js";

import { prettyJson } from "./result-text.js";

import { ToolPolicy, toolPolicy } from "./policy.js";

export { checkEuRange } from "./application/write.js";
export { checkMaxChange } from "./adapters/opcua-write.js";

export class OpcuaTools {
  private readonly runtime: NodeOpcuaRuntime;
  private readonly featureHandlers: ProtocolFeatureHandlers;
  private get capabilities() {
    return this.runtime.capabilities;
  }
  private set capabilities(value: CapabilityAnswers) {
    this.runtime.capabilities = value;
  }
  private get subs() {
    return this.runtime.subs;
  }
  private get events() {
    return this.runtime.events;
  }
  private get metadata() {
    return this.runtime.metadata;
  }
  get warmUpWaitMs() {
    return this.runtime.warmUpWaitMs;
  }
  set warmUpWaitMs(value: number) {
    this.runtime.warmUpWaitMs = value;
  }
  async warmUp() {
    await this.runtime.warmUp();
  }
  startWarmUp() {
    return this.runtime.startWarmUp(() => this.warmUp());
  }
  private awaitConnectionInFlight() {
    return this.runtime.awaitConnectionInFlight();
  }
  private awaitWarmUp() {
    return this.runtime.awaitWarmUp();
  }
  async shutdown() {
    await this.runtime.shutdown();
  }
  private ensureConnection() {
    return this.runtime.ensureConnection();
  }
  private operationLimits() {
    return this.runtime.operationLimits();
  }
  private ensureCapabilities(spec: ToolSpec, args: Record<string, unknown>) {
    return this.runtime.ensureCapabilities(spec, args);
  }
  private requireSession() {
    return this.runtime.requireSession();
  }

  constructor(
    private readonly conn: OpcuaConnection,
    private readonly policy: ToolPolicy = toolPolicy(),
    private readonly audit: AuditSink = new AuditSink()
  ) {
    this.runtime = new NodeOpcuaRuntime(conn);
    this.featureHandlers = new ProtocolFeatureHandlers(
      this.runtime,
      this.policy,
      () => this.operationLimits(),
      () => this.requireSession()
    );
  }

  /** The advertised tool list: the contract, filtered by deployment policy only.
   *
   * No network I/O, no waiting, and the same answer for the life of the process
   * whatever the plant is doing (#140). It used to depend on the capabilities
   * of the session held — `read_event_history` absent, `aggregate_function`
   * withheld — so a process started while the plant was down advertised less
   * than one started while it was up, and nothing portable told a client that
   * had listed once to list again. Clients and models cache tool definitions
   * for the life of a session; what they cache now stays true. A capability the
   * server lacks is reported by the call that needs it (`ensureCapabilities`)
   * and by `get_server_status`.
   *
   * The policy is configuration, fixed when the process starts, so filtering
   * on it does not make the catalogue move. No `notifications/tools/list_changed`
   * is sent, here or on the Python runtime — see docs/architecture.md — and
   * nothing depends on one.
   */
  async listTools(): Promise<Tool[]> {
    return this.policy.visibleTools(CONTRACT.tools).map((tool) => ({
      name: tool.name,
      description: tool.description,
      inputSchema: tool.inputSchema,
      annotations: tool.annotations,
      outputSchema: outputSchema(tool),
    })) satisfies Tool[];
  }

  /** The advertised resource list: taken straight from the contract. */
  listResources(): Resource[] {
    return CONTRACT.resources.map((r) => ({
      uri: r.uri,
      name: r.name,
      description: r.description,
      mimeType: r.mimeType,
    }));
  }

  /** Serve a resources/read request.
   *
   * Deliberately does not touch the OPC UA server: this reports what the
   * subscriptions have already delivered, so it stays readable — and honest —
   * even while the connection is down.
   */
  readResource(uri: string) {
    const resource = CONTRACT.resources.find((r) => r.uri === uri);
    if (!resource) {
      throw new Error(`Unknown resource: ${uri}`);
    }
    return {
      contents: [
        {
          uri: resource.uri,
          mimeType: resource.mimeType,
          text: prettyJson({ [resource.body.recordsKey]: this.subs.list() }),
        },
      ],
    };
  }

  /** Convert one protocol request through the common execution envelope. */
  async callTool(request: { params: { name: string; arguments?: Record<string, unknown> } }) {
    const name = request.params.name,
      args = request.params.arguments ?? {};
    try {
      return await executeTool(
        {
          newCallId,
          waitForConnection: () => this.awaitConnectionInFlight(),
          authorize: (name, args) => this.policy.authorize(name, args),
          allowed: (call) =>
            auditPermission(
              this.audit,
              this.policy,
              this.conn,
              call.name,
              call.arguments,
              call.callId,
              call.attempt
            ),
          after: (call, decision, reason) =>
            auditAfter(
              this.audit,
              this.policy,
              this.conn,
              call.name,
              call.arguments,
              decision,
              call.callId,
              call.attempt,
              reason
            ),
          run: (call) => this.runTool(call),
          normalizeFailure: (_name, error) => error,
          normalizeResult: (result) => result,
        },
        name,
        args
      );
    } catch (error) {
      return { content: [{ type: "text", text: describeError(error) }], isError: true };
    }
  }

  private async runTool(call: ExecutionCall) {
    return await invokeTool(
      {
        endpoint: () => this.conn.endpointUrl,
        session: () => this.conn.sessionId,
        hasConnection: () => true,
        waitForWarmUp: () => this.awaitWarmUp(),
        connect: () => this.ensureConnection(),
        capabilities: (call) => this.ensureCapabilities(call.spec, call.arguments),
        dispatch: async (call) =>
          call.name === "get_server_status"
            ? await this.invokeFeature(call.spec.name, call.arguments, async () =>
                statusResult(await this.featureHandlers.getServerStatus())
              )
            : await this.invokeFeature(call.spec.name, call.arguments),
        isConnectionError,
        reconnect: (session) => this.conn.reconnect(session),
        logRecovery: (resend) =>
          console.error(
            `OPC UA call failed on a dead session; reconnecting${resend ? " and retrying once" : ""}`
          ),
        targets: (call) => describeTargets(call.spec, call.arguments),
        authorize: (call) => this.policy.authorize(call.name, call.arguments),
        allowed: (call) =>
          auditPermission(
            this.audit,
            this.policy,
            this.conn,
            call.name,
            call.arguments,
            call.callId,
            call.attempt
          ),
        denied: (call, reason) =>
          auditAfter(
            this.audit,
            this.policy,
            this.conn,
            call.name,
            call.arguments,
            "denied",
            call.callId,
            call.attempt,
            reason
          ),
      },
      call
    );
  }

  /** Anticipated failures keep their wording; crashes share the Python SDK boundary. */
  private async invokeFeature(
    name: ToolName,
    args: Record<string, unknown>,
    operation = () => this.dispatch(name, args)
  ) {
    try {
      return await operation();
    } catch (error) {
      // Recovery still sees native connection failures, including their causes.
      if (error instanceof ToolFailure || isConnectionError(error)) throw error;
      throw new UnexpectedToolFailure(name, error);
    }
  }

  private dispatch(name: ToolName, args: Record<string, unknown>) {
    return dispatchFeature(name, args, this.featureHandlers);
  }
}
