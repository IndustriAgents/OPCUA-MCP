"""The conformance scenarios, one function each, run once per runtime.

Every scenario talks to the OPC UA server *only* through the MCP tools a
client would call — never through a library of its own — so a pass means the
runtime did the job, not the harness. Each returns an `Outcome`:

* ``pass`` — the behaviour held.
* ``fail`` — it did not. A human then triages it in the config as a project
  bug, a library limitation, a server limitation or an unsupported optional
  feature; until they do, the result says "untriaged" and cannot be published.
* ``unsupported`` — the server itself says it does not offer the feature (no
  endpoint for the mode, no history capability, no aggregate functions). Not a
  failure of anything: OPC UA makes most of this optional.
* ``not-configured`` — the config maps no node for it, or the setup the
  scenario needs (a lab server it can restart, a user account) is missing.

What a scenario may put in `detail` and `evidence`: node-map *keys*, status
code names, data type names, counts and booleans. Never a node id, a value, an
endpoint or a credential — the result file is meant to be published.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from conformance.config import Config, resolve
from conformance.lab import Discovery, LabServer, Pki
from conformance.runtime import Session, open_session

# Well-known namespace-0 nodes (OPC UA Part 5).
SERVER_OBJECT = "ns=0;i=2253"
MAX_NODES_PER_READ = "ns=0;i=11705"
MAX_NODES_PER_WRITE = "ns=0;i=11707"
AGGREGATE_FUNCTIONS = "ns=0;i=2997"

#: The contract's own ceilings on one read and one write (contract/tools.json limits).
CONTRACT_MAX_NODES_PER_READ = 500
CONTRACT_MAX_NODES_PER_WRITE = 100


@dataclass
class Outcome:
    result: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


def passed(detail: str, **evidence: Any) -> Outcome:
    return Outcome("pass", detail, evidence)


def failed(detail: str, **evidence: Any) -> Outcome:
    return Outcome("fail", detail, evidence)


def unsupported(detail: str, **evidence: Any) -> Outcome:
    return Outcome("unsupported", detail, evidence)


def not_configured(detail: str) -> Outcome:
    return Outcome("not-configured", detail)


class Refused(Exception):
    """The runtime could not open a working session — the expected answer in a negative case."""


class Context:
    """Everything a scenario needs, for one runtime against one server."""

    def __init__(
        self,
        config: Config,
        runtime: str,
        pki: Pki,
        discovery: Discovery,
        lab: LabServer | None,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.pki = pki
        self.discovery = discovery
        self.lab = lab
        self.url = config.endpoint_url()
        security = config.security
        self.policy = security.get("policy", "Basic256Sha256")
        default = security.get("default", {})
        self.default_mode = default.get("mode", "None")
        # The policy a secured scenario uses when the default channel is None.
        self.default_policy = default.get("policy", self.policy)
        if self.default_policy == "None":
            self.default_policy = self.policy
        self.build_info: dict[str, Any] | None = None
        #: Where each scenario's runtime stderr goes, for triage; set by the CLI.
        self.log_dir: Path | None = None
        self.scenario = ""
        self._secrets: list[str] = []
        user = config.identities.get("user") or {}
        for key in ("username", "password"):
            value = resolve(user.get(key), required=False) if user else None
            if value:
                self._secrets.append(value)

    # --- configuration -------------------------------------------------------

    def user_credentials(self) -> tuple[str, str] | None:
        user = self.config.identities.get("user")
        if not user:
            return None
        name = resolve(user.get("username"), required=False)
        password = resolve(user.get("password"), required=False)
        if not name or not password:
            return None
        return name, password

    def client_certificate(self, which: str = "client") -> tuple[str, str]:
        supplied = self.config.security.get("clientCertificate")
        if which == "client" and isinstance(supplied, dict):
            return resolve(supplied["cert"]), resolve(supplied["key"])
        return self.pki[which], self.pki[f"{which}_key"]

    def env(
        self,
        *,
        mode: str | None = None,
        policy: str | None = None,
        identity: str = "default",
        pin: str | None = None,
        client: str = "client",
        control: dict[str, str] | None = None,
        password_override: str | None = None,
    ) -> dict[str, str]:
        """The environment a runtime is started with for one scenario.

        Short reconnect waits, so a restart scenario finishes in seconds rather
        than the minutes the production defaults allow.
        """
        mode = mode or self.default_mode
        env = {
            "OPCUA_SERVER_URL": self.url,
            "OPCUA_PROFILE": "observe",
            "OPCUA_RECONNECT_INITIAL_DELAY_MS": "250",
            "OPCUA_RECONNECT_MAX_DELAY_MS": "2000",
            "OPCUA_RECONNECT_MAX_RETRY": "6",
        }
        if mode != "None":
            cert, key = self.client_certificate(client)
            env.update(
                {
                    "OPCUA_SECURITY_POLICY": policy or self.default_policy,
                    "OPCUA_SECURITY_MODE": mode,
                    "OPCUA_CLIENT_CERT": cert,
                    "OPCUA_CLIENT_KEY": key,
                }
            )
            if pin:
                env["OPCUA_SERVER_CERT"] = pin

        if identity == "default":
            identity = "anonymous" if self.config.identities.get("anonymous", True) else "user"
        if identity == "user":
            credentials = self.user_credentials()
            if credentials:
                env["OPCUA_USERNAME"] = credentials[0]
                env["OPCUA_PASSWORD"] = password_override or credentials[1]
        elif identity == "x509":
            env["OPCUA_USER_CERT"] = self.pki["user"]
            env["OPCUA_USER_KEY"] = self.pki["user_key"]

        if control is not None:
            env["OPCUA_PROFILE"] = "operator"
            env.update(control)
            # The control gate (#134) wants a server whose identity is known.
            # Pin the certificate discovery found when there is a secure
            # channel; say so explicitly when there is not.
            if mode == "None":
                env["OPCUA_ALLOW_INSECURE_CONTROL"] = "true"
            elif "OPCUA_SERVER_CERT" not in env:
                if self.discovery.server_certificate:
                    env["OPCUA_SERVER_CERT"] = str(self.discovery.server_certificate)
                else:
                    env["OPCUA_ALLOW_UNVERIFIED_SERVER_CONTROL"] = "true"
        return env

    # --- sessions and node ids -----------------------------------------------

    @asynccontextmanager
    async def session(self, **env_kwargs: Any):
        log = self.log_dir / f"{self.runtime}-{self.scenario}.log" if self.log_dir else None
        async with open_session(self.runtime, self.env(**env_kwargs), log) as session:
            yield session

    async def namespaces(self, session: Session) -> list[str]:
        status = await session.status()
        if not status.get("connected"):
            raise Refused(self.redact(str(status.get("error"))))
        if self.build_info is None and status.get("build_info"):
            self.build_info = status["build_info"]
        return [entry["uri"] for entry in status.get("namespaces") or []]

    async def resolver(self, session: Session) -> Callable[[str], str]:
        """Map the config's ``nsu=<uri>;…`` ids onto this server's current indexes.

        Resolved per session on purpose: a namespace index is only meaningful
        against the NamespaceArray it came from, which is the whole reason
        vendor configs should be written with URIs.
        """
        uris = await self.namespaces(session)

        def resolve_node(node: str) -> str:
            match = re.match(r"^nsu=([^;]+);(.*)$", node)
            if not match:
                return node
            uri, rest = match.groups()
            if uri not in uris:
                raise LookupError(f"namespace {uri!r} is not in the server's NamespaceArray")
            return f"ns={uris.index(uri)};{rest}"

        return resolve_node

    def redact(self, text: str | None, limit: int = 240) -> str:
        """Make tool output safe to publish: no endpoint, node id or secret."""
        if not text:
            return ""
        out = text.replace(self.url, "<endpoint>")
        for secret in self._secrets:
            out = out.replace(secret, "<redacted>")
        out = re.sub(r"nsu=[^;\s\"']+;[isgb]=[^\s,\"'\]]+", "<node>", out)
        out = re.sub(r"ns=\d+;[isgb]=[^\s,\"'\]]+", "<node>", out)
        out = re.sub(r"opc\.tcp://[^\s\"']+", "<endpoint>", out)
        out = " ".join(out.split())
        return out if len(out) <= limit else out[: limit - 1] + "…"


# --- helpers ---------------------------------------------------------------------


def _statuses(records: list[dict]) -> list[str]:
    return [str(r.get("status")) for r in records if isinstance(r, dict)]


def _good(status: Any) -> bool:
    return isinstance(status, str) and status.startswith("Good")


def _bad(status: Any) -> bool:
    return isinstance(status, str) and status.startswith("Bad")


def _json_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, (int, float)):
        return "Number"
    if isinstance(value, str):
        return "String"
    if isinstance(value, list):
        return "Array"
    if isinstance(value, dict):
        return "Object"
    return "Null"


#: How each OPC UA output type is expected to come back in a JSON result.
_EXPECTED_KIND = {
    "Boolean": "Boolean",
    "String": "String",
    "LocalizedText": "String",
    "DateTime": "String",
    **{
        name: "Number"
        for name in (
            "SByte",
            "Byte",
            "Int16",
            "UInt16",
            "Int32",
            "UInt32",
            "Float",
            "Double",
        )
    },
}


async def _read(session: Session, node_ids: list[str]) -> tuple[Any, list[dict]]:
    result = await session.call("read_opcua_nodes", {"node_ids": node_ids})
    return result, [r for r in result.flat() if isinstance(r, dict) and "status" in r]


async def _refused(ctx: Context, **env_kwargs: Any) -> tuple[bool, str]:
    """Try to read the server's state; True if the runtime could not.

    A refusal shows up differently per runtime — Python fails `initialize`
    because it connects first, Node answers per call — so both count.
    """
    try:
        async with ctx.session(**env_kwargs) as session:
            status = await session.status()
            if not status.get("connected"):
                return True, ctx.redact(str(status.get("error")))
            result = await session.call("read_opcua_nodes", {"node_ids": [MAX_NODES_PER_READ]})
            records = [r for r in result.flat() if isinstance(r, dict)]
            if result.is_error or not records or not _good(records[0].get("status")):
                return True, ctx.redact(result.text)
            return False, "session opened and a read succeeded"
    except Exception as error:
        while getattr(error, "exceptions", None):  # an (anyio) exception group
            error = error.exceptions[0]
        return True, ctx.redact(f"{type(error).__name__}: {error}")


# --- connection and discovery ---------------------------------------------------


async def connect_status(ctx: Context) -> Outcome:
    async with ctx.session() as session:
        status = await session.status()
    if not status.get("connected"):
        return failed(f"not connected: {ctx.redact(str(status.get('error')))}")
    build = status.get("build_info") or {}
    ctx.build_info = ctx.build_info or build
    if status.get("server_state") != "Running":
        return failed(f"server_state is {status.get('server_state')!r}, not Running")
    if not build.get("product_name"):
        return failed("BuildInfo has no product name")
    return passed(
        "connected; ServerStatus and BuildInfo read",
        server_state=status["server_state"],
        diagnostics_published=status.get("diagnostics") is not None,
    )


async def connect_namespaces(ctx: Context) -> Outcome:
    if not ctx.config.namespaces:
        return not_configured("no namespace URIs in the config")
    async with ctx.session() as session:
        uris = await ctx.namespaces(session)
    missing = [key for key, uri in ctx.config.namespaces.items() if uri not in uris]
    if missing:
        return failed(f"namespace URIs not in the NamespaceArray: {', '.join(missing)}")
    return passed(
        f"all {len(ctx.config.namespaces)} configured namespace URIs resolved",
        namespace_count=len(uris),
    )


async def connect_endpoint_discovery(ctx: Context) -> Outcome:
    """A secure channel with the server certificate taken from GetEndpoints.

    No pinned certificate and no OPCUA_APPLICATION_URI: the runtime must find
    the server's certificate through discovery and announce the ApplicationUri
    from its own certificate, which is what a server that checks it verifies.
    """
    mode = next(
        (m for m in ("SignAndEncrypt", "Sign") if m in ctx.config.security.get("modes", [])), None
    )
    if mode is None:
        return not_configured("no secured mode configured")
    if not ctx.discovery.offers(ctx.policy, mode):
        return unsupported(f"server offers no {ctx.policy}/{mode} endpoint")
    refused, reason = await _refused(ctx, mode=mode, policy=ctx.policy)
    if refused:
        return failed(f"secured session via discovery failed: {reason}")
    return passed(f"{ctx.policy}/{mode} with the certificate from GetEndpoints")


# --- channel security -------------------------------------------------------------


def _mode_scenario(mode: str) -> Callable[[Context], Awaitable[Outcome]]:
    async def run(ctx: Context) -> Outcome:
        if mode not in ctx.config.security.get("modes", []):
            return not_configured(f"security mode {mode} not selected in the config")
        policy = "None" if mode == "None" else ctx.policy
        if ctx.discovery.endpoints and not ctx.discovery.offers(policy, mode):
            return unsupported(f"server offers no {policy}/{mode} endpoint")
        scalar = next(iter((ctx.config.node("scalars") or {}).values()), None)
        async with ctx.session(mode=mode, policy=policy if mode != "None" else None) as session:
            status = await session.status()
            if not status.get("connected"):
                return failed(f"not connected: {ctx.redact(str(status.get('error')))}")
            security = str(status.get("security", ""))
            if not security.startswith(f"policy={policy} mode={mode}"):
                return failed(f"status reports {ctx.redact(security.split(' user=')[0])}")
            if scalar:
                resolve_node = await ctx.resolver(session)
                _, records = await _read(session, [resolve_node(scalar)])
                if not records or not _good(records[0].get("status")):
                    return failed(f"read over {mode} returned {_statuses(records)}")
        return passed(f"{policy}/{mode}: connected and read")

    return run


async def security_server_pinned(ctx: Context) -> Outcome:
    mode = _secure_mode(ctx)
    if mode not in ctx.config.security.get("modes", []):
        return not_configured("no secured mode configured")
    if ctx.discovery.server_certificate is None:
        return not_configured("discovery returned no server certificate to pin")
    async with ctx.session(mode=mode, pin=str(ctx.discovery.server_certificate)) as session:
        status = await session.status()
    identity = status.get("server_identity") or {}
    if not status.get("connected"):
        return failed(f"pinned certificate refused: {ctx.redact(str(status.get('error')))}")
    if not identity.get("server_authenticated"):
        return failed(f"connected but server_identity says {identity}")
    return passed("pinned server certificate accepted and reported as authenticated")


async def security_server_impostor(ctx: Context) -> Outcome:
    mode = _secure_mode(ctx)
    if mode not in ctx.config.security.get("modes", []):
        return not_configured("no secured mode configured")
    refused, reason = await _refused(ctx, mode=mode, pin=ctx.pki["impostor"])
    if not refused:
        return failed("a pinned certificate that is not the server's was accepted")
    return passed("an impostor server certificate was refused", reason=reason)


async def security_client_untrusted(ctx: Context) -> Outcome:
    trust = ctx.config.security.get("clientTrust", "unknown")
    if trust != "enforced":
        return not_configured(f"server client-certificate trust is {trust!r}, not enforced")
    refused, reason = await _refused(ctx, mode=_secure_mode(ctx), client="untrusted")
    if not refused:
        return failed("a client certificate the server does not trust got a working session")
    return passed("an untrusted client certificate was refused", reason=reason)


# --- user identity ------------------------------------------------------------------


async def identity_anonymous(ctx: Context) -> Outcome:
    if not ctx.config.identities.get("anonymous", True):
        return not_configured("anonymous access not selected in the config")
    if ctx.discovery.endpoints and "Anonymous" not in ctx.discovery.token_types():
        return unsupported("server offers no anonymous user token")
    refused, reason = await _refused(ctx, identity="anonymous")
    return failed(f"anonymous session failed: {reason}") if refused else passed("anonymous session")


def _secure_mode(ctx: Context) -> str:
    """The channel credentials travel over: the default one if it is secured.

    A password on an unsecured channel is a configuration both runtimes warn
    about, and not one a conformance run should normalise by testing on it.
    """
    return ctx.default_mode if ctx.default_mode != "None" else "SignAndEncrypt"


async def identity_username(ctx: Context) -> Outcome:
    if ctx.user_credentials() is None:
        return not_configured("no username/password environment variables set")
    refused, reason = await _refused(ctx, identity="user", mode=_secure_mode(ctx))
    if refused:
        return failed(f"username session failed: {reason}")
    return passed("username/password session")


async def identity_username_rejected(ctx: Context) -> Outcome:
    credentials = ctx.user_credentials()
    if credentials is None:
        return not_configured("no username/password environment variables set")
    refused, reason = await _refused(
        ctx,
        identity="user",
        mode=_secure_mode(ctx),
        password_override=credentials[1] + "-not-the-password",
    )
    if not refused:
        return failed("a wrong password got a working session")
    return passed("a wrong password was refused", reason=reason)


async def identity_x509(ctx: Context) -> Outcome:
    if not ctx.config.identities.get("x509"):
        return not_configured("X.509 user identity not selected in the config")
    if ctx.discovery.endpoints and "Certificate" not in ctx.discovery.token_types():
        return unsupported("server offers no X.509 user token")
    refused, reason = await _refused(ctx, identity="x509", mode=_secure_mode(ctx))
    return (
        failed(f"X.509 user session failed: {reason}") if refused else passed("X.509 user session")
    )


# --- browse --------------------------------------------------------------------------


async def browse_children(ctx: Context) -> Outcome:
    root = ctx.config.node("browseRoot")
    if not root:
        return not_configured("nodes.browseRoot not mapped")
    async with ctx.session() as session:
        resolve_node = await ctx.resolver(session)
        result = await session.call("browse_opcua_nodes", {"node_id": resolve_node(root)})
    record = result.first()
    if result.is_error or not isinstance(record, dict):
        return failed(f"browse failed: {ctx.redact(result.text)}")
    count = len(record.get("nodes", []))
    if count == 0:
        return failed("browse returned no children")
    return passed(f"{count} children", truncated=record.get("truncated"))


async def browse_continuation(ctx: Context) -> Outcome:
    folder = ctx.config.node("largeFolder")
    if not folder:
        return not_configured("nodes.largeFolder not mapped")
    minimum = int(folder.get("minChildren", 0))
    async with ctx.session() as session:
        resolve_node = await ctx.resolver(session)
        result = await session.call(
            "browse_opcua_nodes",
            {"node_id": resolve_node(folder["node"]), "max_nodes": min(minimum + 100, 5000)},
            timeout=120,
        )
    record = result.first()
    if result.is_error or not isinstance(record, dict):
        return failed(f"browse failed: {ctx.redact(result.text)}")
    count = len(record.get("nodes", []))
    if count < minimum:
        return failed(
            f"{count} of at least {minimum} children returned",
            truncated=record.get("truncated"),
        )
    return passed(f"{count} children (expected at least {minimum})")


# --- values ----------------------------------------------------------------------------


async def _read_map(ctx: Context, key: str) -> tuple[dict[str, str], list[dict]] | None:
    mapping = ctx.config.node(key)
    if not mapping:
        return None
    if isinstance(mapping, list):
        mapping = {f"{key}[{i}]": node for i, node in enumerate(mapping)}
    async with ctx.session() as session:
        resolve_node = await ctx.resolver(session)
        _, records = await _read(session, [resolve_node(n) for n in mapping.values()])
    return mapping, records


async def values_scalar(ctx: Context) -> Outcome:
    read = await _read_map(ctx, "scalars")
    if read is None:
        return not_configured("nodes.scalars not mapped")
    mapping, records = read
    problems = []
    for (expected, _), record in zip(mapping.items(), records, strict=False):
        if not _good(record.get("status")):
            problems.append(f"{expected}: {record.get('status')}")
        elif record.get("data_type") != expected:
            problems.append(f"{expected}: data_type {record.get('data_type')}")
        elif record.get("value") is None:
            problems.append(f"{expected}: null value")
    if len(records) != len(mapping):
        problems.append(f"{len(records)} records for {len(mapping)} nodes")
    if problems:
        return failed("; ".join(problems), types=len(mapping))
    return passed(
        f"{len(mapping)} scalar types read with the right data_type", types=sorted(mapping)
    )


async def values_array(ctx: Context) -> Outcome:
    read = await _read_map(ctx, "arrays")
    if read is None:
        return not_configured("nodes.arrays not mapped")
    mapping, records = read
    problems = []
    for (expected, _), record in zip(mapping.items(), records, strict=False):
        if not _good(record.get("status")):
            problems.append(f"{expected}[]: {record.get('status')}")
        elif not isinstance(record.get("value"), list):
            problems.append(f"{expected}[]: value is {_json_kind(record.get('value'))}, not Array")
        elif record.get("data_type") != expected:
            problems.append(f"{expected}[]: data_type {record.get('data_type')}")
    if problems:
        return failed("; ".join(problems))
    return passed(f"{len(mapping)} array types read as JSON arrays", types=sorted(mapping))


async def values_structure(ctx: Context) -> Outcome:
    read = await _read_map(ctx, "structures")
    if read is None:
        return not_configured("nodes.structures not mapped")
    mapping, records = read
    problems, shapes = [], {}
    for (name, _), record in zip(mapping.items(), records, strict=False):
        shapes[name] = _json_kind(record.get("value"))
        if not _good(record.get("status")):
            problems.append(f"{name}: {record.get('status')}")
        elif record.get("value") is None:
            problems.append(f"{name}: no value decoded")
        # A structure is a JSON object (OPC UA Part 6's JSON encoding). A
        # string is some library's rendering of it: readable, perhaps, but not
        # the fields, and not the same text on both runtimes.
        elif shapes[name] != "Object":
            problems.append(f"{name}: {shapes[name]}, not an object")
    if problems:
        return failed("; ".join(problems), shapes=shapes)
    return passed(f"{len(mapping)} structured values decoded to objects", shapes=shapes)


# --- write and operation limits ---------------------------------------------------------


def _write_control(ctx: Context, nodes: list[str]) -> dict[str, str]:
    return {"OPCUA_ALLOWED_WRITE_NODES": ",".join(nodes)}


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-9)
    return a == b


async def write_readback(ctx: Context) -> Outcome:
    writable = ctx.config.node("writable")
    if not writable:
        return not_configured("nodes.writable not mapped")
    nodes = [w["node"] for w in writable]
    async with ctx.session(control=_write_control(ctx, nodes)) as session:
        resolve_node = await ctx.resolver(session)
        ids = [resolve_node(n) for n in nodes]
        result = await session.call(
            "write_opcua_nodes",
            {
                "nodes": [
                    {"node_id": i, "value": w["value"]} for i, w in zip(ids, writable, strict=False)
                ]
            },
        )
        statuses = _statuses(result.flat())
        if result.is_error or not statuses or not all(_good(s) for s in statuses):
            return failed(f"write returned {statuses or ctx.redact(result.text)}")
        _, records = await _read(session, ids)
    mismatched = [
        f"writable[{i}]"
        for i, (w, r) in enumerate(zip(writable, records, strict=False))
        if not _close(w["value"], r.get("value"))
    ]
    if mismatched:
        return failed(f"read-back differs for {', '.join(mismatched)}")
    return passed(f"{len(writable)} nodes written in one batch and read back")


async def _limit(session: Session, node: str) -> int | None:
    _, records = await _read(session, [node])
    if not records or not _good(records[0].get("status")):
        return None
    value = records[0].get("value")
    return int(value) if isinstance(value, (int, float)) else None


async def limits_read(ctx: Context) -> Outcome:
    """A batch read larger than the server's MaxNodesPerRead.

    A server with a limit answers an oversized Read with BadTooManyOperations
    for the whole request. A client that knows the limit splits the batch; one
    that does not loses every value in it, so this is the case to run.
    """
    pool = list((ctx.config.node("scalars") or {}).values())
    pool += list((ctx.config.node("arrays") or {}).values())
    if not pool:
        return not_configured("nodes.scalars not mapped")
    async with ctx.session() as session:
        resolve_node = await ctx.resolver(session)
        limit = await _limit(session, MAX_NODES_PER_READ)
        size = limit + 10 if limit and limit + 10 <= CONTRACT_MAX_NODES_PER_READ else 100
        ids = [resolve_node(pool[i % len(pool)]) for i in range(size)]
        result, records = await _read(session, ids)
    statuses = _statuses(records)
    good = sum(1 for s in statuses if _good(s))
    evidence = {"server_max_nodes_per_read": limit, "batch": size, "good": good}
    if good != size:
        worst = sorted({s for s in statuses if not _good(s)}) or [ctx.redact(result.text, 120)]
        return failed(f"{good} of {size} reads Good; others: {', '.join(worst)}", **evidence)
    return passed(f"{size} nodes in one call (server limit {limit or 'none'})", **evidence)


async def limits_write(ctx: Context) -> Outcome:
    """A write batch larger than the server's MaxNodesPerWrite.

    Unlike a read, a write is refused rather than split, by design (#139): the
    parts of a split write could land or fail on their own, and a caller that
    asked for one write must not get half of one. So the behaviour that passes
    is a refusal of the whole batch, before anything is sent, that names the
    server's limit — not BadTooManyOperations from the server itself, and not
    a batch cut up behind the caller's back.
    """
    writable = ctx.config.node("writable")
    if not writable:
        return not_configured("nodes.writable not mapped")
    nodes = [w["node"] for w in writable]
    async with ctx.session(control=_write_control(ctx, nodes)) as session:
        resolve_node = await ctx.resolver(session)
        limit = await _limit(session, MAX_NODES_PER_WRITE)
        # Over the limit when the limit is one a tool call can reach; a plain
        # batched write otherwise, as the read scenario does.
        size = limit + 5 if limit and limit + 5 <= CONTRACT_MAX_NODES_PER_WRITE else 20
        batch = [
            {
                "node_id": resolve_node(writable[i % len(writable)]["node"]),
                "value": writable[i % len(writable)]["value"],
            }
            for i in range(size)
        ]
        result = await session.call("write_opcua_nodes", {"nodes": batch})
    statuses = _statuses(result.flat())
    good = sum(1 for s in statuses if _good(s))
    evidence = {"server_max_nodes_per_write": limit, "batch": size, "good": good}
    over_limit = bool(limit) and size > limit
    if over_limit:
        # The contract's tooManyWritesForServer sentence names the limit.
        refused = result.is_error and f"at most {limit}" in result.text and not statuses
        if refused:
            return passed(
                f"{size}-node write refused whole, naming the server's limit of {limit}",
                refused_before_sending=True,
                **evidence,
            )
        if good == size:
            return failed(f"{size} writes past a server limit of {limit} all reported Good")
        detail = sorted({s for s in statuses if not _good(s)}) or [ctx.redact(result.text, 120)]
        return failed(f"over-limit write was not refused up front: {', '.join(detail)}", **evidence)
    if good != size:
        worst = sorted({s for s in statuses if not _good(s)}) or [ctx.redact(result.text, 120)]
        return failed(f"{good} of {size} writes Good; others: {', '.join(worst)}", **evidence)
    return passed(f"{size} writes in one call (server limit {limit or 'none'})", **evidence)


# --- methods ------------------------------------------------------------------------------


def _method_control(entry: dict) -> dict[str, str]:
    return {"OPCUA_ALLOWED_METHODS": f"{entry['object']}|{entry['method']}"}


async def method_call(ctx: Context) -> Outcome:
    entry = ctx.config.node("method")
    if not entry:
        return not_configured("nodes.method not mapped")
    async with ctx.session(control=_method_control(entry)) as session:
        resolve_node = await ctx.resolver(session)
        result = await session.call(
            "call_opcua_method",
            {
                "object_node_id": resolve_node(entry["object"]),
                "method_node_id": resolve_node(entry["method"]),
                "arguments": entry.get("arguments", []),
            },
        )
    record = result.first()
    if result.is_error or not isinstance(record, dict) or not _good(record.get("status")):
        return failed(f"call failed: {ctx.redact(result.text)}")
    outputs = record.get("outputs") or []
    expected = entry.get("outputTypes", [])
    kinds = [_json_kind(o) for o in outputs]
    wanted = [_EXPECTED_KIND.get(t, "Object") for t in expected]
    if len(outputs) != len(expected) or kinds != wanted:
        return failed(f"outputs came back as {kinds}, expected {wanted}")
    return passed(f"{len(expected)} input and {len(outputs)} output arguments", output_kinds=kinds)


# --- history --------------------------------------------------------------------------------


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


#: What a server puts where a bounding value would go when there is none.
#: Both client libraries ask for bounds (ReturnBounds=true) by default, so a
#: window that opens before the first stored value starts with one of these.
#: It is a placeholder the server was asked for, not a stored reading (#172).
BOUND_STATUSES = {"BadBoundNotFound", "BadBoundNotSupported"}


def _history_records(result: Any) -> list[dict]:
    return [r for r in result.flat() if isinstance(r, dict) and "timestamp" in r]


def _history_values(records: list[dict]) -> tuple[list[dict], int]:
    """Stored readings, and how many bound placeholders came with them."""
    values = [r for r in records if r.get("status") not in BOUND_STATUSES]
    return values, len(records) - len(values)


async def history_raw(ctx: Context) -> Outcome:
    node = ctx.config.node("historized")
    if not node:
        return not_configured("nodes.historized not mapped")
    async with ctx.session() as session:
        tools = await session.tools()
        if "read_opcua_history" not in tools:
            return unsupported("server does not advertise AccessHistoryDataCapability")
        resolve_node = await ctx.resolver(session)
        now = datetime.now(timezone.utc)
        result = await session.call(
            "read_opcua_history",
            {
                "node_id": resolve_node(node),
                "start_time": _iso(now - timedelta(minutes=10)),
                "end_time": _iso(now),
                "num_values": 10,
            },
        )
    values, bounds = _history_values(_history_records(result))
    if result.is_error or not values:
        return failed(f"no history returned: {ctx.redact(result.text)}")
    if not all(_good(r.get("status")) for r in values):
        return failed(f"history statuses {sorted(set(_statuses(values)))}")
    placeholders = f", plus {bounds} BadBoundNotFound bound placeholder(s)" if bounds else ""
    return passed(
        f"{len(values)} raw values{placeholders}", count=len(values), bound_placeholders=bounds
    )


async def history_continuation(ctx: Context) -> Outcome:
    """More raw values than the server returns in one response.

    The config says how many the server pages at; asking for several pages'
    worth tells a client that follows continuation points from one that
    silently returns the first page. Two answers pass: the runtime pages
    through itself, or it says the result is a prefix (`completeness.complete`
    false, with a `continuation`) — and following that continuation, as a
    client would, then reaches every value asked for.
    """
    node = ctx.config.node("historized")
    paging = ctx.config.node("historyPaging")
    if not node or not paging:
        return not_configured("nodes.historized or nodes.historyPaging not mapped")
    wanted = int(paging["numValues"])
    async with ctx.session() as session:
        tools = await session.tools()
        if "read_opcua_history" not in tools:
            return unsupported("server does not advertise AccessHistoryDataCapability")
        resolve_node = await ctx.resolver(session)
        now = datetime.now(timezone.utc)
        arguments = {
            "node_id": resolve_node(node),
            "start_time": _iso(now - timedelta(minutes=30)),
            "end_time": _iso(now),
            "num_values": wanted,
        }
        result = await session.call("read_opcua_history", arguments)
        values, bounds = _history_values(_history_records(result))
        first = result.completeness()
        evidence = {
            "requested": wanted,
            "server_page_size": paging.get("serverPageSize"),
            "first_response": len(values),
            "bound_placeholders": bounds,
            "completeness_reported": first is not None,
        }
        if result.is_error:
            return failed(f"read failed: {ctx.redact(result.text)}", **evidence)
        if len(values) >= wanted:
            return passed(f"{len(values)} values: the runtime paged through itself", **evidence)
        if first is None or first.get("complete") is not False:
            return failed(
                f"{len(values)} of {wanted} values returned, reported as complete", **evidence
            )
        evidence["reasons"] = first.get("reasons")
        if first.get("continuation") is None:
            return failed(
                f"{len(values)} of {wanted} values, truncation reported but no continuation",
                **evidence,
            )
        # Follow it the way the contract says: merge, call again, and skip
        # records already seen by timestamp. A follow-up repeats more than the
        # record at its inclusive start — with bounds requested, the server
        # also returns the value just before it — so each call asks for the
        # full count again rather than for exactly what is still missing.
        seen = {v.get("timestamp") for v in values}
        collected, calls, current = len(values), 1, first
        while collected < wanted and current and current.get("continuation") and calls < 20:
            arguments = {**arguments, **current["continuation"]}
            more = await session.call("read_opcua_history", arguments)
            calls += 1
            fresh = [
                v
                for v in _history_values(_history_records(more))[0]
                if v.get("timestamp") not in seen
            ]
            if more.is_error:
                current = None
                break
            current = more.completeness()
            if not fresh:
                break
            seen.update(v.get("timestamp") for v in fresh)
            collected += len(fresh)
    evidence.update(calls=calls, collected=collected)
    # The server may simply hold fewer than asked for — a lab server restarted
    # by an earlier scenario starts its history again. Following the
    # continuation to a response that says it is complete is the whole
    # behaviour; only stopping short of that is a failure.
    reached_end = bool(current and current.get("complete") is True)
    evidence["reached_end"] = reached_end
    if collected < wanted and reached_end and collected > evidence["first_response"]:
        return passed(
            f"first response {evidence['first_response']} of {wanted}, reported incomplete; "
            f"the continuation reached the end of the server's {collected} values "
            f"in {calls} calls",
            **evidence,
        )
    if collected < wanted:
        return failed(
            f"truncation reported, but following the continuation reached {collected} of {wanted}",
            **evidence,
        )
    return passed(
        f"first response {evidence['first_response']} of {wanted}, reported incomplete; "
        f"the continuation reached all {wanted} in {calls} calls",
        **evidence,
    )


async def history_aggregate(ctx: Context) -> Outcome:
    node = ctx.config.node("historized")
    if not node:
        return not_configured("nodes.historized not mapped")
    async with ctx.session() as session:
        tools = await session.tools()
        schema = tools.get("read_opcua_history") or {}
        if "aggregate_function" not in (schema.get("properties") or {}):
            return unsupported("server advertises no aggregate functions")
        resolve_node = await ctx.resolver(session)
        now = datetime.now(timezone.utc)
        result = await session.call(
            "read_opcua_history",
            {
                "node_id": resolve_node(node),
                "start_time": _iso(now - timedelta(seconds=60)),
                "end_time": _iso(now),
                "aggregate_function": "Average",
                "processing_interval": 10000,
            },
        )
    records = _history_records(result)
    if result.is_error or not records:
        return failed(f"no aggregate values: {ctx.redact(result.text)}")
    return passed(f"{len(records)} Average intervals", count=len(records))


# --- subscriptions ---------------------------------------------------------------------------


async def _subscribe_and_collect(
    ctx: Context, session: Session, node: str, seconds: float, **options: Any
) -> tuple[Any, int]:
    resolve_node = await ctx.resolver(session)
    subscribed = await session.call(
        "subscribe_opcua_nodes",
        {"node_ids": [resolve_node(node)], "publishing_interval": 250, **options},
    )
    if subscribed.is_error:
        return subscribed, -1
    await asyncio.sleep(seconds)
    listed = await session.call("list_subscriptions")
    changes = sum(int(r.get("change_count", 0)) for r in listed.flat() if isinstance(r, dict))
    return subscribed, changes


async def subscription_data_change(ctx: Context) -> Outcome:
    node = ctx.config.node("changing")
    if not node:
        return not_configured("nodes.changing not mapped")
    async with ctx.session() as session:
        subscribed, changes = await _subscribe_and_collect(ctx, session, node, 4)
    if changes < 0:
        return failed(f"subscribe failed: {ctx.redact(subscribed.text)}")
    if changes < 2:
        return failed(f"{changes} data changes in 4 s from a node that changes continuously")
    return passed(f"{changes} data changes in 4 s", changes=changes)


async def subscription_deadband(ctx: Context) -> Outcome:
    node = ctx.config.node("changing")
    if not node:
        return not_configured("nodes.changing not mapped")
    async with ctx.session() as session:
        subscribed, changes = await _subscribe_and_collect(
            ctx, session, node, 4, deadband_type="absolute", deadband_value=0.5
        )
    if changes < 0:
        return failed(f"absolute deadband refused: {ctx.redact(subscribed.text)}")
    if changes < 1:
        return failed("no data changes with an absolute deadband below the step size")
    return passed(f"absolute deadband accepted; {changes} changes", changes=changes)


# --- events and alarms --------------------------------------------------------------------------


async def events_subscribe(ctx: Context) -> Outcome:
    events = ctx.config.node("events")
    if not events:
        return not_configured("nodes.events not mapped")
    trigger = events.get("trigger")
    control = _method_control(trigger) if trigger else None
    async with ctx.session(control=control) as session:
        resolve_node = await ctx.resolver(session)
        source = resolve_node(events.get("source", SERVER_OBJECT))
        subscribed = await session.call("subscribe_events", {"node_id": source})
        if subscribed.is_error:
            return failed(f"subscribe_events failed: {ctx.redact(subscribed.text)}")
        if trigger:
            fired = await session.call(
                "call_opcua_method",
                {
                    "object_node_id": resolve_node(trigger["object"]),
                    "method_node_id": resolve_node(trigger["method"]),
                    "arguments": trigger.get("arguments", []),
                },
            )
            if fired.is_error:
                return failed(f"event trigger method failed: {ctx.redact(fired.text)}")
        received: list[dict] = []
        for _ in range(8):
            await asyncio.sleep(1)
            read = await session.call("read_events", {"node_id": source})
            received += [r for r in read.flat() if isinstance(r, dict) and "event_id" in r]
            if received:
                break
    if not received:
        return failed("no event arrived within 8 s")
    # How many distinct types, not which: an event type is a node id, and a
    # vendor's own types live in its own namespace.
    types = len({str(r.get("event_type")) for r in received})
    return passed(f"{len(received)} events received", event_types=types)


async def events_history(ctx: Context) -> Outcome:
    events = ctx.config.node("events")
    if not events:
        return not_configured("nodes.events not mapped")
    async with ctx.session() as session:
        tools = await session.tools()
        if "read_event_history" not in tools:
            return unsupported("server does not advertise AccessHistoryEventsCapability")
        resolve_node = await ctx.resolver(session)
        now = datetime.now(timezone.utc)
        result = await session.call(
            "read_event_history",
            {
                "node_id": resolve_node(events.get("source", SERVER_OBJECT)),
                "start_time": _iso(now - timedelta(minutes=10)),
                "end_time": _iso(now),
                "num_values": 50,
            },
        )
    if result.is_error:
        return failed(f"read_event_history failed: {ctx.redact(result.text)}")
    count = sum(1 for r in result.flat() if isinstance(r, dict) and "event_id" in r)
    return passed(f"{count} historical events", count=count)


async def _rearm(ctx: Context, session: Session, alarms: dict) -> Any:
    rearm = alarms.get("rearm")
    if not rearm:
        return None
    resolve_node = await ctx.resolver(session)
    result = await session.call(
        "call_opcua_method",
        {
            "object_node_id": resolve_node(rearm["object"]),
            "method_node_id": resolve_node(rearm["method"]),
            "arguments": rearm.get("arguments", []),
        },
    )
    await asyncio.sleep(1)
    return result


async def alarms_list(ctx: Context) -> Outcome:
    alarms = ctx.config.node("alarms")
    if not alarms:
        return not_configured("nodes.alarms not mapped")
    control = _method_control(alarms["rearm"]) if alarms.get("rearm") else None
    async with ctx.session(control=control) as session:
        await _rearm(ctx, session, alarms)
        resolve_node = await ctx.resolver(session)
        result = await session.call(
            "list_active_alarms", {"node_id": resolve_node(alarms.get("source", SERVER_OBJECT))}
        )
    if result.is_error:
        return failed(f"list_active_alarms failed: {ctx.redact(result.text)}")
    listed = [r for r in result.flat() if isinstance(r, dict) and "event_id" in r]
    if not listed:
        return failed("no retained condition listed, though the config says one is active")
    with_condition = sum(1 for r in listed if r.get("condition_id"))
    return passed(f"{len(listed)} retained conditions", with_condition_id=with_condition)


async def alarms_acknowledge(ctx: Context) -> Outcome:
    alarms = ctx.config.node("alarms")
    if not alarms or not alarms.get("acknowledge"):
        return not_configured("nodes.alarms.acknowledge not enabled")
    control = {"OPCUA_ALLOW_ACKNOWLEDGE_ALARMS": "true"}
    if alarms.get("rearm"):
        control.update(_method_control(alarms["rearm"]))
    async with ctx.session(control=control) as session:
        await _rearm(ctx, session, alarms)
        resolve_node = await ctx.resolver(session)
        listed = await session.call(
            "list_active_alarms", {"node_id": resolve_node(alarms.get("source", SERVER_OBJECT))}
        )
        candidates = [
            r
            for r in listed.flat()
            if isinstance(r, dict) and r.get("event_id") and r.get("acked") is False
        ]
        if not candidates:
            return failed("no unacknowledged condition to acknowledge")
        target = candidates[0]
        result = await session.call(
            "acknowledge_alarm",
            {"event_id": target["event_id"], "comment": "OPCUA-MCP conformance run"},
        )
    record = result.first()
    if result.is_error or not isinstance(record, dict) or not _good(record.get("status")):
        return failed(f"acknowledge failed: {ctx.redact(result.text)}")
    return passed("acknowledged a retained condition by event_id")


# --- authorization and negative cases -------------------------------------------------------------


async def authz_denied_write(ctx: Context) -> Outcome:
    entry = ctx.config.node("deniedWrite")
    if not entry:
        return not_configured("nodes.deniedWrite not mapped")
    async with ctx.session(
        control=_write_control(ctx, [entry["node"]]), identity=entry.get("identity", "default")
    ) as session:
        resolve_node = await ctx.resolver(session)
        result = await session.call(
            "write_opcua_nodes",
            {"nodes": [{"node_id": resolve_node(entry["node"]), "value": entry["value"]}]},
        )
        status = await session.status()
    statuses = _statuses(result.flat())
    if not statuses or not _bad(statuses[0]):
        return failed(
            f"write the server should refuse returned {statuses or ctx.redact(result.text)}"
        )
    if not status.get("connected"):
        return failed("session lost after a refused write")
    return passed(f"refused per item with {statuses[0]}; session intact", status=statuses[0])


async def authz_denied_read(ctx: Context) -> Outcome:
    node = ctx.config.node("deniedRead")
    if not node:
        return not_configured("nodes.deniedRead not mapped")
    async with ctx.session() as session:
        resolve_node = await ctx.resolver(session)
        _, records = await _read(session, [resolve_node(node)])
    status = records[0].get("status") if records else None
    if not _bad(status):
        return failed(f"read the server should refuse returned {status}")
    if records[0].get("value") is not None:
        return failed("a refused read still carried a value")
    return passed(f"refused per item with {status}", status=status)


async def authz_denied_method(ctx: Context) -> Outcome:
    entry = ctx.config.node("deniedMethod")
    if not entry:
        return not_configured("nodes.deniedMethod not mapped")
    async with ctx.session(control=_method_control(entry)) as session:
        resolve_node = await ctx.resolver(session)
        result = await session.call(
            "call_opcua_method",
            {
                "object_node_id": resolve_node(entry["object"]),
                "method_node_id": resolve_node(entry["method"]),
                "arguments": entry.get("arguments", []),
            },
        )
        status = await session.status()
    record = result.first()
    if isinstance(record, dict) and _good(record.get("status")):
        return failed("a method the server should refuse was executed")
    if not status.get("connected"):
        return failed("session lost after a refused call")
    reason = record.get("status") if isinstance(record, dict) else ctx.redact(result.text, 120)
    return passed(f"refused: {reason}")


async def authz_unknown_node(ctx: Context) -> Outcome:
    unknown = ctx.config.node("unknown")
    scalar = next(iter((ctx.config.node("scalars") or {}).values()), None)
    if not unknown or not scalar:
        return not_configured("nodes.unknown or nodes.scalars not mapped")
    async with ctx.session() as session:
        resolve_node = await ctx.resolver(session)
        result, records = await _read(session, [resolve_node(scalar), resolve_node(unknown)])
    statuses = _statuses(records)
    if result.is_error or len(statuses) != 2:
        return failed(f"a batch with one unknown node failed as a whole: {ctx.redact(result.text)}")
    if not _good(statuses[0]) or not _bad(statuses[1]):
        return failed(f"statuses {statuses}; expected the known node Good, the unknown Bad")
    return passed(f"unknown node answered per item with {statuses[1]}", status=statuses[1])


# --- recovery (needs a lab server the harness can restart) ------------------------


async def _wait_connected(
    ctx: Context, session: Session, node: str, seconds: float = 45
) -> str | None:
    """Read `node` until Good or out of time; the last status, None on success."""
    deadline = asyncio.get_running_loop().time() + seconds
    last = "no attempt"
    while asyncio.get_running_loop().time() < deadline:
        try:
            resolve_node = await ctx.resolver(session)
            _, records = await _read(session, [resolve_node(node)])
            if records and _good(records[0].get("status")):
                return None
            last = str(records[0].get("status")) if records else "no record"
        except Exception as error:
            last = ctx.redact(f"{type(error).__name__}: {error}", 120)
        await asyncio.sleep(1)
    return last


async def recovery_restart(ctx: Context) -> Outcome:
    scalar = next(iter((ctx.config.node("scalars") or {}).values()), None)
    if ctx.lab is None or not scalar:
        return not_configured("needs a lab server the harness controls, and nodes.scalars")
    async with ctx.session() as session:
        if await _wait_connected(ctx, session, scalar, 10):
            return failed("no Good read before the restart")
        await asyncio.to_thread(ctx.lab.stop)
        started = asyncio.get_running_loop().time()
        down = await session.call(
            "read_opcua_nodes", {"node_ids": [MAX_NODES_PER_READ]}, timeout=90
        )
        answered_in = asyncio.get_running_loop().time() - started
        await asyncio.to_thread(ctx.lab.start)
        last = await _wait_connected(ctx, session, scalar)
    down_records = [r for r in down.flat() if isinstance(r, dict)]
    down_good = bool(down_records) and _good(down_records[0].get("status"))
    evidence = {"answered_while_down_s": round(answered_in, 1)}
    if down_good:
        return failed("a read while the server was down reported Good", **evidence)
    if last:
        return failed(f"no Good read within 45 s of the server returning: {last}", **evidence)
    return passed(
        "read failed cleanly while down and recovered on the same MCP session", **evidence
    )


async def recovery_subscription(ctx: Context) -> Outcome:
    node = ctx.config.node("changing")
    if ctx.lab is None or not node:
        return not_configured("needs a lab server the harness controls, and nodes.changing")
    async with ctx.session() as session:
        subscribed, before = await _subscribe_and_collect(ctx, session, node, 2)
        if before < 1:
            return failed(f"no data changes before the restart: {ctx.redact(subscribed.text)}")
        await asyncio.to_thread(ctx.lab.restart)
        await _wait_connected(ctx, session, node)
        await asyncio.sleep(3)
        listed = await session.call("list_subscriptions")
        records = [r for r in listed.flat() if isinstance(r, dict) and "changes" in r]
        after = 0
        for record in records:
            restart_marker = datetime.now(timezone.utc) - timedelta(seconds=3)
            for change in record.get("changes") or []:
                stamp = (
                    change.get("server_timestamp")
                    or change.get("source_timestamp")
                    or change.get("timestamp")
                )
                if (
                    stamp
                    and datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) >= restart_marker
                ):
                    after += 1
    if after < 1:
        return failed("the subscription delivered nothing after the server restarted")
    return passed(f"subscription resumed; {after} changes after the restart", changes_after=after)


async def recovery_namespace_reorder(ctx: Context) -> Outcome:
    """The server comes back with its namespaces in a different order.

    Only a lab server can be made to do this on demand. A runtime that cached
    the NamespaceArray from its first session would keep reporting the old
    index, and an id built from it would name a different node.
    """
    scalar = next(iter((ctx.config.node("scalars") or {}).values()), None)
    if ctx.lab is None or not ctx.lab.can("reorderedCommand") or not scalar:
        return not_configured("needs a lab server with lab.reorderedCommand")
    uri = re.match(r"^nsu=([^;]+);", scalar)
    if not uri:
        return not_configured("nodes.scalars must use nsu= ids for this scenario")
    try:
        async with ctx.session() as session:
            before = (await ctx.namespaces(session)).index(uri.group(1))
            await asyncio.to_thread(ctx.lab.restart, "reorderedCommand")
            last = await _wait_connected(ctx, session, scalar)
            after = (await ctx.namespaces(session)).index(uri.group(1))
    finally:
        await asyncio.to_thread(ctx.lab.restart)
    evidence = {"index_before": before, "index_after": after}
    if last:
        return failed(f"no Good read of a re-resolved node after the reorder: {last}", **evidence)
    if after == before:
        return failed("NamespaceArray still reports the pre-restart index", **evidence)
    return passed(f"namespace index moved {before} -> {after} and was re-resolved", **evidence)


# --- the registry --------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    id: str
    group: str
    title: str
    run: Callable[[Context], Awaitable[Outcome]]


#: Report groups, in matrix column order — the same idea as the required-suite
#: groups (tests/required_suite.py): a column is a subsystem, and a result has
#: to show which subsystems it actually covered.
GROUPS = (
    "Connection",
    "Security",
    "Identity",
    "Browse",
    "Values",
    "Write & limits",
    "Methods",
    "History",
    "Subscriptions",
    "Events & alarms",
    "Authorization",
    "Recovery",
)

SCENARIOS: tuple[Scenario, ...] = (
    Scenario("connect.status", "Connection", "ServerStatus and BuildInfo", connect_status),
    Scenario("connect.namespaces", "Connection", "Namespace URIs resolve", connect_namespaces),
    Scenario(
        "connect.endpoint-discovery",
        "Connection",
        "Server certificate from GetEndpoints; ApplicationUri from the client certificate",
        connect_endpoint_discovery,
    ),
    Scenario("security.none", "Security", "SecurityMode None", _mode_scenario("None")),
    Scenario("security.sign", "Security", "SecurityMode Sign", _mode_scenario("Sign")),
    Scenario(
        "security.sign-and-encrypt",
        "Security",
        "SecurityMode SignAndEncrypt",
        _mode_scenario("SignAndEncrypt"),
    ),
    Scenario(
        "security.server-pinned", "Security", "Pinned server certificate", security_server_pinned
    ),
    Scenario(
        "security.server-impostor",
        "Security",
        "Wrong pinned server certificate is refused",
        security_server_impostor,
    ),
    Scenario(
        "security.client-untrusted",
        "Security",
        "Untrusted client certificate is refused",
        security_client_untrusted,
    ),
    Scenario("identity.anonymous", "Identity", "Anonymous user", identity_anonymous),
    Scenario("identity.username", "Identity", "Username and password", identity_username),
    Scenario(
        "identity.username-rejected",
        "Identity",
        "Wrong password is refused",
        identity_username_rejected,
    ),
    Scenario("identity.x509", "Identity", "X.509 user certificate", identity_x509),
    Scenario("browse.children", "Browse", "Browse one level", browse_children),
    Scenario(
        "browse.continuation",
        "Browse",
        "Large folder across continuation points",
        browse_continuation,
    ),
    Scenario("values.scalar", "Values", "Scalar built-in types", values_scalar),
    Scenario("values.array", "Values", "Array values", values_array),
    Scenario("values.structure", "Values", "Structured (ExtensionObject) values", values_structure),
    Scenario("write.readback", "Write & limits", "Batched write and read-back", write_readback),
    Scenario("limits.read", "Write & limits", "Read batch above MaxNodesPerRead", limits_read),
    Scenario("limits.write", "Write & limits", "Write batch above MaxNodesPerWrite", limits_write),
    Scenario("method.call", "Methods", "Input and output arguments", method_call),
    Scenario("history.raw", "History", "Raw history", history_raw),
    Scenario(
        "history.continuation",
        "History",
        "Raw history across continuation points",
        history_continuation,
    ),
    Scenario("history.aggregate", "History", "Aggregate history", history_aggregate),
    Scenario(
        "subscription.data-change",
        "Subscriptions",
        "Data-change notifications",
        subscription_data_change,
    ),
    Scenario("subscription.deadband", "Subscriptions", "Absolute deadband", subscription_deadband),
    Scenario("events.subscribe", "Events & alarms", "Live events", events_subscribe),
    Scenario("events.history", "Events & alarms", "Historical events", events_history),
    Scenario(
        "alarms.list", "Events & alarms", "Retained conditions (ConditionRefresh)", alarms_list
    ),
    Scenario(
        "alarms.acknowledge", "Events & alarms", "Acknowledge a condition", alarms_acknowledge
    ),
    Scenario("authz.denied-write", "Authorization", "Server refuses a write", authz_denied_write),
    Scenario("authz.denied-read", "Authorization", "Server refuses a read", authz_denied_read),
    Scenario(
        "authz.denied-method", "Authorization", "Server refuses a method call", authz_denied_method
    ),
    Scenario(
        "authz.unknown-node", "Authorization", "Unknown node answered per item", authz_unknown_node
    ),
    Scenario("recovery.restart", "Recovery", "Server restart during a session", recovery_restart),
    Scenario(
        "recovery.subscription",
        "Recovery",
        "Subscription survives a server restart",
        recovery_subscription,
    ),
    Scenario(
        "recovery.namespace-reorder",
        "Recovery",
        "Namespace order changes across a restart",
        recovery_namespace_reorder,
    ),
)

SCENARIO_IDS = tuple(s.id for s in SCENARIOS)
