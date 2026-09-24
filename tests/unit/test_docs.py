"""The documentation's own facts: links, and the counts nobody regenerates (#149).

The reference tables, the tool count and the release version are generated
(`npm run config:generate` in packages/server-node; CI runs `config:check`).
What generation cannot keep true is the hand-written prose around them, so this
module checks the parts of it that can be checked mechanically:

* every relative link, and every link into this repository on GitHub, names a
  file that exists — and, for a Markdown file, a heading that exists;
* no live document states a tool count other than the contract's. The count is
  generated inside the tool-reference blocks; a count typed anywhere else is
  the copy that went stale (#149 found "13 tools" in the roadmap at 15).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote

import pytest
from conftest import ROOT

CONTRACT = json.loads((ROOT / "contract" / "tools.json").read_text(encoding="utf-8"))
TOOL_COUNT = len(CONTRACT["tools"])

#: Where this repository's files are served from on GitHub. A package README links
#: this way because it is also read on npmjs.com and pypi.org.
REPO_URL = re.compile(r"^https://github\.com/IndustriAgents/OPCUA-MCP/(?:blob|tree)/main/(.+)$")

#: Installed or built, not written here. Dot-directories are skipped too.
SKIP_DIRS = {"node_modules", "build", "dist", "build-mcpb", "build-sea"}

DOCS = sorted(
    path
    for path in ROOT.rglob("*.md")
    if not SKIP_DIRS.intersection(path.relative_to(ROOT).parts)
    and not any(part.startswith(".") for part in path.relative_to(ROOT).parts[:-1])
)

#: Records of what was true when they were written, where an old count is a fact
#: about the past rather than a claim about now.
HISTORICAL = {"CHANGELOG.md"}
HISTORICAL_DIRS = {"archive"}


def _is_historical(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    return (
        rel.as_posix() in HISTORICAL
        or bool(HISTORICAL_DIRS.intersection(rel.parts))
        or re.fullmatch(r"ROADMAP-\d+\.\d+\.\d+\.md", rel.name) is not None
    )


def _prose(text: str) -> str:
    """`text` without fenced code blocks and inline code, where a `[x](y)` or a
    heading-like line is an example rather than a link or a heading."""
    text = re.sub(r"^(```|~~~).*?^\1", "", text, flags=re.S | re.M)
    return re.sub(r"`[^`\n]*`", "", text)


def _slug(heading: str) -> str:
    """A heading's anchor as GitHub computes it."""
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link's text only
    heading = re.sub(r"<[^>]+>", "", heading)
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


_anchor_cache: dict[Path, set[str]] = {}


def _anchors(path: Path) -> set[str]:
    if path not in _anchor_cache:
        text = re.sub(r"^(```|~~~).*?^\1", "", path.read_text(encoding="utf-8"), flags=re.S | re.M)
        seen: dict[str, int] = {}
        anchors: set[str] = set()
        for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", text, flags=re.M):
            slug = _slug(heading)
            # GitHub disambiguates a repeated heading as slug-1, slug-2, ...
            anchors.add(f"{slug}-{seen[slug]}" if slug in seen else slug)
            seen[slug] = seen.get(slug, 0) + 1
        anchors.update(re.findall(r"""<a\s+(?:name|id)=["']([^"']+)["']""", text))
        _anchor_cache[path] = anchors
    return _anchor_cache[path]


#: `[text](target)` or `[text](<target> "title")`: the target.
LINK = re.compile(r"\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def _links(path: Path) -> list[str]:
    return LINK.findall(_prose(path.read_text(encoding="utf-8")))


def _broken(doc: Path, target: str) -> str | None:
    """Why `target`, linked from `doc`, does not resolve — or None if it does."""
    match = REPO_URL.match(target)
    if match:
        base, rest = ROOT, match.group(1)
    elif re.match(r"^[a-z][a-z0-9+.-]*:", target, flags=re.I):
        return None  # another site, mailto:, ...: not this repository's to check
    else:
        base, rest = doc.parent, target
    path_part, _, anchor = rest.partition("#")
    path_part = path_part.split("?", 1)[0]
    resolved = (base / unquote(path_part)).resolve() if path_part else doc
    if not resolved.exists():
        return f"{target}: no such file"
    if anchor and resolved.suffix == ".md" and anchor.lower() not in _anchors(resolved):
        return f"{target}: {resolved.relative_to(ROOT).as_posix()} has no heading #{anchor}"
    return None


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_links_resolve(doc):
    broken = [reason for target in _links(doc) if (reason := _broken(doc, target))]
    assert not broken, "broken links:\n  " + "\n  ".join(broken)


_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen"]
_TEENS += ["seventeen", "eighteen", "nineteen", "twenty"]
NUMBER_WORDS = {word: n for n, word in enumerate(_UNITS + _TEENS)}
#: "15 tools", "fifteen MCP tools", "the same fifteen tools" — a claim about how
#: many tools there are, rather than "two tools" in a sentence about a pair.
TOOL_COUNT_CLAIM = re.compile(
    r"\b(\d+|" + "|".join(NUMBER_WORDS) + r")\*{0,2}\s+(?:MCP\s+|OPC UA\s+)?tools\b",
    flags=re.I,
)


@pytest.mark.parametrize(
    "doc", [d for d in DOCS if not _is_historical(d)], ids=lambda p: p.relative_to(ROOT).as_posix()
)
def test_no_stale_tool_count(doc):
    stale = set()
    for match in TOOL_COUNT_CLAIM.finditer(_prose(doc.read_text(encoding="utf-8"))):
        word = match.group(1).lower()
        count = NUMBER_WORDS[word] if word in NUMBER_WORDS else int(word)
        # "two tools" and the like describe a subset; only a count near the size
        # of the whole surface is a claim about the whole surface.
        if count != TOOL_COUNT and count > 5:
            stale.add(match.group(0))
    assert not stale, f"{sorted(stale)} — the contract has {TOOL_COUNT} tools"


def test_the_checks_can_fail():
    """The checks above pass by finding nothing; make sure they can find something."""
    doc = ROOT / "README.md"
    assert _broken(doc, "docs/no-such-file.md") == "docs/no-such-file.md: no such file"
    assert "has no heading" in _broken(doc, "docs/examples.md#no-such-heading")
    assert _broken(doc, "docs/examples.md#read_opcua_nodes") is None
    assert "no such file" in _broken(
        doc, "https://github.com/IndustriAgents/OPCUA-MCP/blob/main/docs/no-such-file.md"
    )
    assert TOOL_COUNT_CLAIM.search("the same thirteen tools")
