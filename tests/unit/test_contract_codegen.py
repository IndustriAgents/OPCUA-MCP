"""Standard schemas, static surfaces and cross-runtime normalized validation."""

from __future__ import annotations

import copy
import importlib.util
import json
import random
import subprocess

import pytest
from conftest import ROOT
from jsonschema import Draft202012Validator
from opcua_mcp_server.contract import CONTRACT
from opcua_mcp_server.generated_contract import TOOL_INPUT_TYPES, TOOL_NAMES, TOOL_RESULT_TYPES
from opcua_mcp_server.validation import ValidationRefusal, validate_arguments

SPEC = importlib.util.spec_from_file_location(
    "contract_codegen", ROOT / "scripts/contract_codegen.py"
)
GEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GEN)


def test_generated_surface_matches_contract_and_is_current():
    names = tuple(t["name"] for t in CONTRACT["tools"])
    assert names == TOOL_NAMES
    assert set(TOOL_INPUT_TYPES) == set(TOOL_RESULT_TYPES) == set(names)
    for t in CONTRACT["tools"]:
        assert set(TOOL_INPUT_TYPES[t["name"]].__annotations__) == set(
            t["inputSchema"]["properties"]
        )
        assert TOOL_INPUT_TYPES[t["name"]].__required_keys__ == frozenset(
            t["inputSchema"].get("required", [])
        )
    for path, content in GEN.render(CONTRACT).items():
        assert path.read_text(encoding="utf-8") == content, f"Regenerate {path}"


def test_metadata_and_embedded_schemas_use_explicit_standard_draft():
    meta = json.loads((ROOT / "contract/contract.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(meta)
    Draft202012Validator(meta).validate(CONTRACT)
    for schema in json.loads((ROOT / "contract/schemas.json").read_text(encoding="utf-8"))[
        "$defs"
    ].values():
        assert schema["$schema"] == GEN.DRAFT
        GEN.check_schema(schema)


@pytest.mark.parametrize(
    "keyword", ["minitem", "additionalProperty", "unrecognizedKeyword", "$ref"]
)
def test_unimplemented_or_misspelled_keywords_fail_generation(keyword):
    with pytest.raises(ValueError, match="Unsupported schema keywords"):
        GEN.check_schema({"type": "object", "properties": {"nested": {keyword: 1}}})


def test_differential_property_corpus_has_identical_codes_paths_and_text():
    """Thousands of seeded JSON mutations, using the installed Node validator.

    This is independent of the runtime helpers: generate arbitrary values and
    compare actual engine outcomes, including simultaneous nested violations.
    """
    if not (ROOT / "packages/server-node/build/validation.js").exists():
        pytest.fail("Build the Node server before running contract differential checks")
    rng = random.Random(138)
    values = [
        None,
        False,
        True,
        0,
        1,
        -1,
        -1.0,
        1e-5,
        -1e-5,
        1e20,
        1.5,
        "",
        "bad",
        [],
        {},
        [None],
        {"constructor": 1},
    ]

    def arbitrary(depth=0):
        value = copy.deepcopy(rng.choice(values))
        if depth < 2 and rng.random() < 0.25:
            return [arbitrary(depth + 1) for _ in range(rng.randrange(4))]
        return value

    cases = []
    fixture = json.loads(
        (ROOT / "tests/fixtures/argument-validation.json").read_text(encoding="utf-8")
    )["cases"]
    specs = {t["name"]: t for t in CONTRACT["tools"]}
    for c in fixture:
        cases.append(
            {"tool": c["tool"], "schema": specs[c["tool"]]["inputSchema"], "args": c["arguments"]}
        )
    for t in CONTRACT["tools"]:
        props = list(t["inputSchema"]["properties"])
        for _ in range(150):
            args = {p: arbitrary() for p in props if rng.random() < 0.8}
            if rng.random() < 0.2:
                args[rng.choice(["constructor", "__proto__", "typo"])] = arbitrary()
            cases.append({"tool": t["name"], "schema": t["inputSchema"], "args": args})
    for constraint in [
        {"type": "number", "maximum": 2},
        {"type": "number", "exclusiveMinimum": 0},
        {"type": "number", "multipleOf": 0.5},
        {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^a"},
        {
            "type": "array",
            "uniqueItems": True,
            "minItems": 2,
            "maxItems": 3,
            "items": {"type": "integer"},
        },
        {
            "type": "object",
            "properties": {"value": {"type": "integer", "maximum": 10}},
            "required": ["value"],
            "additionalProperties": False,
        },
    ]:
        schema = {
            "$schema": GEN.DRAFT,
            "type": "object",
            "properties": {"test": constraint},
            "required": ["test"],
            "additionalProperties": False,
        }
        GEN.check_schema(schema)
        for _ in range(150):
            cases.append({"tool": "probe", "schema": schema, "args": {"test": arbitrary()}})
    expected = []
    for c in cases:
        try:
            validate_arguments(c["tool"], c["schema"], c["args"])
            expected.append(None)
        except ValidationRefusal as e:
            expected.append({"code": e.code, "argument": e.argument, "text": str(e)})
    module = (ROOT / "packages/server-node/build/validation.js").as_uri()
    script = f"""
      import {{validateArguments}} from {json.dumps(module)};
      import {{readFileSync}} from 'node:fs';
      const cases=JSON.parse(readFileSync(0,'utf8'));
      const outcomes=cases.map(c=>{{try {{validateArguments(c.tool,c.schema,c.args);return null;}}
       catch(e) {{return {{code:e.code,argument:e.argument,text:e.message}};}}}});
      process.stdout.write(JSON.stringify(outcomes));
    """
    actual = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        input=json.dumps(cases),
        text=True,
        capture_output=True,
        check=True,
    )
    observed = json.loads(actual.stdout)
    for index, (py, node) in enumerate(zip(expected, observed, strict=True)):
        assert py == node, f"Case {index}: {cases[index]}: Python {py}, Node {node}"
