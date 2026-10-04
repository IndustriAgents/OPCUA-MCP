# ruff: noqa: E501
"""Offline type comparison: python-opcua 0.98.13 vs asyncua 2.0.1, no server.

What the port has to normalise, measured on the two libraries side by side:
status-code tables, exception types, the wire bytes of every write-coercion
case, the decoding of every value-encoding case, DateTime handling,
ExtensionObjects (#171) and the certificate loaders.

    uv run --no-sync --with asyncua==2.0.1 python docs/asyncua-spike/probe_types.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import inspect
import warnings
from datetime import datetime, timezone

import asyncua.ua as a
import opcua.ua as o
from _common import ROOT, load_json, report
from asyncua.common.utils import Buffer as ABuffer
from asyncua.ua import ua_binary as ab
from opcua.common.utils import Buffer as OBuffer
from opcua.ua import ua_binary as ob

CONTRACT = load_json("contract/tools.json")


def status_codes() -> None:
    def names(module):
        return {n: v for n, v in vars(module.StatusCodes).items() if not n.startswith("_")}

    ours, theirs = names(o), names(a)
    dead = CONTRACT["deadSession"]["statusCodeNames"]["names"]
    missing = [n for n in dead if n not in theirs or theirs[n] != ours.get(n)]
    report(
        "supported" if not missing else "bug",
        "status.dead-session-names",
        f"all {len(dead)} deadSession.statusCodeNames resolve in both libraries to the same "
        f"number; missing/different in asyncua: {missing or 'none'}",
    )
    only_old = sorted(set(ours) - set(theirs))
    only_new = sorted(set(theirs) - set(ours))
    renumbered = sorted(n for n in set(ours) & set(theirs) if ours[n] != theirs[n])
    report(
        "differs",
        "status.table",
        f"python-opcua {len(ours)} names, asyncua {len(theirs)}; only in python-opcua: "
        f"{only_old[:8]}{'...' if len(only_old) > 8 else ''} ({len(only_old)}); only in asyncua: "
        f"{len(only_new)} (e.g. {only_new[:4]}); same name, different number: {renumbered}",
    )
    unknown = 0x80FF0000
    report(
        "differs" if o.StatusCode(unknown).name != a.StatusCode(unknown).name else "supported",
        "status.unknown-name",
        f"an unlisted code 0x80FF0000: python-opcua .name={o.StatusCode(unknown).name!r}, "
        f"asyncua .name={a.StatusCode(unknown).name!r}",
    )
    good_clamped = a.StatusCodes.GoodClamped
    report(
        "supported",
        "status.is-good",
        f"GoodClamped is_good: python-opcua {o.StatusCode(good_clamped).is_good()}, "
        f"asyncua {a.StatusCode(good_clamped).is_good()}",
    )


def exceptions() -> None:
    code = a.StatusCodes.BadNodeIdUnknown
    old, new = o.UaStatusCodeError(code), a.UaStatusCodeError(code)
    report(
        "differs",
        "errors.status-exception",
        f"python-opcua raises {type(old).__module__}.{type(old).__name__} (code={old.code:#x}), "
        f"str={str(old)!r}; asyncua raises {type(new).__module__}.{type(new).__name__}, a subclass "
        f"of UaStatusCodeError={isinstance(new, a.UaStatusCodeError)}, code={new.code:#x}, "
        f"str={str(new)!r}. Classification by .code works on both; the text differs",
    )
    report(
        "differs",
        "errors.timeout-type",
        "asyncua waits with asyncio.wait_for, so a request timeout is asyncio.TimeoutError: "
        f"the builtin TimeoutError on 3.11+ ({asyncio.TimeoutError is TimeoutError} here), NOT "
        "an OSError subclass on 3.10 (requires-python is >=3.10); python-opcua raised "
        "concurrent.futures.TimeoutError. connection.py's _DEAD_SESSION_TYPES must name "
        "asyncio.TimeoutError explicitly",
    )


def to_asyncua(value):
    """The asyncua twin of a python-opcua value object, for wire comparison."""
    if isinstance(value, list):
        return [to_asyncua(v) for v in value]
    if isinstance(value, o.NodeId):
        return a.NodeId.from_string(value.to_string())
    if isinstance(value, o.LocalizedText):
        return a.LocalizedText(value.Text, value.Locale)
    if isinstance(value, o.QualifiedName):
        return a.QualifiedName(value.Name, value.NamespaceIndex)
    return value


def write_coercion() -> None:
    from opcua_mcp_server.variant_codec import convert_for_variant

    same = differ = 0
    samples = []
    for case in load_json("tests/fixtures/write-coercion.json")["cases"]:
        if "expected" not in case:
            continue
        variant_type = o.VariantType[case["type"]]
        raw = convert_for_variant(case["value"], variant_type, case.get("array", False))
        old = ob.variant_to_binary(o.Variant(raw, variant_type))
        try:
            new = ab.variant_to_binary(a.Variant(to_asyncua(raw), a.VariantType[case["type"]]))
        except Exception as error:
            new = f"{type(error).__name__}: {error}"
        if old == new:
            same += 1
        else:
            differ += 1
            samples.append(
                f"{case['type']} {case['value']!r}: {new if isinstance(new, str) else 'bytes differ'}"
            )
    report(
        "supported" if not differ else "differs",
        "values.write-wire-bytes",
        f"write-coercion.json: {same} accepted cases encode to identical wire bytes in both "
        f"libraries, {differ} differ {samples[:5]}",
    )


def value_encoding() -> None:
    spec = importlib.util.spec_from_file_location(
        "test_records", ROOT / "tests/unit/test_records.py"
    )
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.path.insert(0, str(ROOT / "tests" / "unit"))
    sys.path.insert(0, str(ROOT / "tests"))
    spec.loader.exec_module(module)
    from opcua_mcp_server import variant_to_json

    cases = {c["name"]: c for c in load_json("tests/fixtures/value-encoding.json")["cases"]}
    ok, bad, types = 0, [], []
    for name, native in module.NATIVE.items():
        decoded = ab.variant_from_binary(ABuffer(ob.variant_to_binary(native)))
        got = variant_to_json(decoded)
        case = cases[name]
        want = case.get("expected", case.get("expectedPattern"))
        if "expectedPattern" in case:
            import re

            matched = isinstance(got, str) and re.match(want, got)
        else:
            matched = got == want
        if matched:
            ok += 1
        else:
            bad.append(f"{name}: {got!r} != {want!r}")
        old = ob.variant_from_binary(OBuffer(ob.variant_to_binary(native))).Value
        if type(old).__name__ != type(decoded.Value).__name__ or (
            isinstance(old, datetime) and old.tzinfo != decoded.Value.tzinfo
        ):
            types.append(
                f"{name}: {type(old).__name__}{'' if not isinstance(old, datetime) else '(tz=' + str(old.tzinfo) + ')'}"
                f" -> {type(decoded.Value).__name__}"
                f"{'' if not isinstance(decoded.Value, datetime) else '(tz=' + str(decoded.Value.tzinfo) + ')'}"
            )
    report(
        "supported" if not bad else "differs",
        "values.read-json",
        f"value-encoding.json: {ok}/{len(module.NATIVE)} cases, encoded by python-opcua and "
        f"decoded by asyncua, give the expected JSON through records.variant_to_json; "
        f"mismatches: {bad or 'none'}",
    )
    report(
        "differs" if types else "supported",
        "values.native-types",
        f"decoded Python types that change: {types or 'none'}",
    )


def datetimes() -> None:
    naive = datetime(2026, 4, 23, 17, 40)
    aware = naive.replace(tzinfo=timezone.utc)
    same = ab.variant_to_binary(a.Variant(naive, a.VariantType.DateTime)) == ab.variant_to_binary(
        a.Variant(aware, a.VariantType.DateTime)
    )
    back = ab.variant_from_binary(
        ABuffer(ab.variant_to_binary(a.Variant(aware, a.VariantType.DateTime)))
    )
    report(
        "differs",
        "datetime.aware",
        f"asyncua decodes DateTime as {back.Value!r}; python-opcua as naive UTC. A naive "
        f"datetime written through asyncua is taken as UTC (same bytes as aware UTC: {same}), "
        f"as python-opcua does, so writes agree; reads must be normalised by the port",
    )
    zero = ab.variant_from_binary(ABuffer(b"\x0d" + b"\0" * 8)).Value  # DateTime, 0 ticks
    old_zero = ob.variant_from_binary(OBuffer(b"\x0d" + b"\0" * 8)).Value
    report(
        "note",
        "datetime.zero",
        f"DateTime 0 ticks (the 'no timestamp' value) decodes in asyncua as {zero!r}, "
        f"in python-opcua as {old_zero!r}",
    )
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        a.DataValue(a.Variant(1.0))
        a.Variant(datetime.now(timezone.utc), a.VariantType.DateTime)
        ab.variant_to_binary(a.Variant(datetime.now(timezone.utc), a.VariantType.DateTime))
        asyncua_warnings = [
            str(w.message)[:60] for w in seen if issubclass(w.category, DeprecationWarning)
        ]
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        o.DataValue(o.Variant(1.0))
        ob.variant_to_binary(o.Variant(datetime.utcnow(), o.VariantType.DateTime))
        opcua_warnings = [
            str(w.message)[:60] for w in seen if issubclass(w.category, DeprecationWarning)
        ]
    report(
        "supported" if not asyncua_warnings else "differs",
        "datetime.deprecations",
        f"DeprecationWarnings building a DataValue and encoding a DateTime: asyncua "
        f"{len(asyncua_warnings)}, python-opcua {len(opcua_warnings)} {opcua_warnings[:1]}",
    )


def extension_objects() -> None:
    old = o.Range()
    old.Low, old.High = -50.0, 250.0
    wire = ob.extensionobject_to_binary(old)
    new = ab.extensionobject_from_binary(ABuffer(wire))
    eu = o.EUInformation()
    eu.NamespaceUri, eu.UnitId = "http://www.opcfoundation.org/UA/units/un/cefact", 4408652
    eu.DisplayName, eu.Description = o.LocalizedText("°C"), o.LocalizedText("degree Celsius")
    new_eu = ab.extensionobject_from_binary(ABuffer(ob.extensionobject_to_binary(eu)))
    report(
        "differs",
        "values.extension-object",
        f"Range {{-50, 250}}: python-opcua str()={str(old)!r} (fields lost, #171); asyncua decodes "
        f"{type(new).__name__} dataclass={dataclasses.is_dataclass(new)}, str()={str(new)!r}, "
        f"fields={[f.name for f in dataclasses.fields(new)]}; EUInformation -> {str(new_eu)[:90]!r}. "
        f"A Part 6 JSON encoder can walk dataclasses.fields() generically",
    )
    unknown = ab.extensionobject_from_binary(
        ABuffer(b"\x01\x00\x39\x30\x01" + (4).to_bytes(4, "little") + b"abcd")
    )  # ns=0;i=12345, binary body: a type neither library knows
    report(
        "note",
        "values.extension-object-unknown",
        f"an ExtensionObject of an unknown type decodes as {type(unknown).__name__} "
        f"(TypeId={unknown.TypeId.to_string()}, Body={unknown.Body!r}); "
        f"Client.load_data_type_definitions() registers server types at runtime",
    )


def library_surface() -> None:
    from asyncua import Client, Node
    from asyncua.common.subscription import Subscription
    from asyncua.crypto import security_policies, uacrypto

    policies = sorted(n[len("SecurityPolicy"):] for n in dir(security_policies) if n.startswith("SecurityPolicy") and n not in ("SecurityPolicy", "SecurityPolicyFactory", "SecurityPolicyType"))  # fmt: skip
    report(
        "supported",
        "security.policies",
        f"asyncua SecurityPolicy classes: {policies} (contract names Aes128_Sha256_RsaOaep / "
        f"Aes256_Sha256_RsaPss map to the underscore-free class names)",
    )
    report(
        "differs",
        "security.pem-by-extension",
        f"uacrypto.load_certificate is a coroutine ({inspect.iscoroutinefunction(uacrypto.load_certificate)}); "
        f"PEM is chosen by file suffix, or by extension= when given, bytes default to DER "
        f"(_is_pem_format). Sniffing content and passing extension= explicitly removes the "
        f"certificate-file-encoding difference",
    )
    renamed = {
        "Node.server -> Node.session": "self.session" in inspect.getsource(Node.__init__),
        "uaclient.get_attributes -> read_attributes": hasattr(Client("opc.tcp://x").uaclient, "read_attributes"),
        "Node.get_value -> read_value": hasattr(Node, "read_value"),
        "Node.history_read_events": hasattr(Node, "history_read_events"),
        "Subscription.subscribe_events(sourcenode, evtypes, evfilter, queuesize, where_clause_generation)": "evfilter" in inspect.signature(Subscription.subscribe_events).parameters,
    }  # fmt: skip
    report("differs", "api.renames", f"{renamed}")


status_codes()
exceptions()
write_coercion()
value_encoding()
datetimes()
extension_objects()
library_surface()
