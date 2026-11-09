"""Record and render the README demo: an agent reading the simulated plant.

Two steps, so the animation is always built from real responses:

    # 1. With the mock plant running, record real tool calls through the Python bridge
    uv run --no-sync opcua-mock-server &
    uv run --no-sync python scripts/readme_demo.py capture

    # 2. Render the GIF from that recording (needs Pillow; gifsicle optional)
    uv run --no-sync --with pillow python scripts/readme_demo.py render

`capture` launches the Python MCP server over stdio under the read-only
`observe` profile, makes the three tool calls the demo shows, and writes their
structured results to docs/assets/demo-session.json. `render` draws the chat
from that file: the question and the assistant's prose are scripted, but every
tool name, argument and value comes from the recording, and the numbers in the
answer are computed from it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT / "docs" / "assets" / "demo-session.json"
GIF = ROOT / "docs" / "assets" / "demo.gif"
ENDPOINT = "opc.tcp://localhost:4840/freeopcua/server/"
TRY_URL = "github.com/IndustriAgents/OPCUA-MCP#try-it-without-a-plant"

QUESTION = "What are the line's sensors reading right now? Is the temperature stable?"

READ_NODES = {
    "ns=2;i=3": "Temperature",
    "ns=2;i=4": "Pressure",
    "ns=2;i=5": "FlowRate",
    "ns=2;i=10": "MotorSpeed",
}
CALLS = [
    (
        "browse_opcua_nodes",
        {"browse_path": "/Objects/IndustrialControlSystem/Sensors", "node_class": "Variable"},
    ),
    ("read_opcua_nodes", {"node_ids": list(READ_NODES)}),
    ("read_opcua_history", {"node_id": "ns=2;i=3", "num_values": 6}),
]


# --------------------------------------------------------------------------- capture


async def _capture(endpoint: str) -> list[dict]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command="uv",
        args=["run", "--no-sync", "opcua-mcp-server"],
        cwd=str(ROOT),
        env={**os.environ, "OPCUA_SERVER_URL": endpoint, "OPCUA_PROFILE": "observe"},
    )
    calls = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for name, arguments in CALLS:
            result = await session.call_tool(name, arguments)
            if result.is_error:
                text = "".join(getattr(c, "text", "") for c in result.content)
                raise SystemExit(f"{name} failed against {endpoint}: {text}")
            calls.append(
                {"tool": name, "arguments": arguments, "result": result.structured_content}
            )
    return calls


def capture(args: argparse.Namespace) -> None:
    calls = asyncio.run(_capture(args.endpoint))
    session = {
        "note": (
            "Real responses from the Python bridge (profile=observe) against the bundled "
            "mock plant, recorded by scripts/readme_demo.py capture. Input to the README demo."
        ),
        "calls": calls,
    }
    args.session.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.session.relative_to(ROOT)}")


# --------------------------------------------------------------------------- facts


def _facts(session: dict) -> dict:
    """Everything the animation shows, taken from the recording."""
    browse, read, history = (call["result"]["result"] for call in session["calls"])
    sensors = [n["browse_name"].split(":", 1)[-1] for n in browse["nodes"]]
    readings = [
        {
            "name": READ_NODES.get(r["node_id"], r["node_id"]),
            "node_id": r["node_id"],
            "value": r["value"],
            "status": r["status"],
            "time": r["source_timestamp"][11:19],
        }
        for r in read
    ]
    temps = [h["value"] for h in history]
    stamps = [datetime.fromisoformat(h["timestamp"].replace("Z", "+00:00")) for h in history]
    span = abs((stamps[0] - stamps[-1]).total_seconds())
    return {
        "sensors": sensors,
        "readings": readings,
        "history": [(h["timestamp"][11:19], h["value"], h["status"]) for h in history],
        "t_min": min(temps),
        "t_max": max(temps),
        "t_mean": statistics.fmean(temps),
        "span": span,
    }


def _answer(f: dict) -> str:
    by_name = {r["name"]: r["value"] for r in f["readings"]}
    good = all(r["status"] == "Good" for r in f["readings"])
    quality = "All four report Good quality. " if good else ""
    deviation = max(abs(v - f["t_mean"]) for _, v, _ in f["history"])
    steady = deviation / abs(f["t_mean"]) < 0.1 if f["t_mean"] else False
    verdict = "Temperature is steady" if steady else "Temperature is moving"
    return (
        f"{quality}Temperature {by_name['Temperature']:.1f}, pressure "
        f"{by_name['Pressure']:.1f}, flow rate {by_name['FlowRate']:.1f} and motor speed "
        f"{by_name['MotorSpeed']:.1f} (the simulator publishes no engineering units).\n\n"
        f"{verdict}: its last {len(f['history'])} readings, over {f['span']:.0f} seconds, "
        f"stayed between {f['t_min']:.1f} and {f['t_max']:.1f}, never more than "
        f"{deviation:.1f} from their {f['t_mean']:.1f} mean. Nothing was written: this "
        "session is read-only."
    )


# --------------------------------------------------------------------------- render

W, H = 960, 600
PAD = 28
BG = (15, 17, 23)
PANEL = (24, 27, 36)
BORDER = (44, 49, 63)
TEXT = (230, 233, 240)
MUTED = (139, 147, 167)
ACCENT = (110, 168, 254)
USER_BG = (37, 70, 140)
GOOD = (86, 211, 140)
TOOL = (240, 180, 90)
BAR = (72, 110, 170)
FPS = 12

FONT_CANDIDATES = {
    "sans": [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ],
    "bold": [
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
    ],
    "mono": [
        "/System/Library/Fonts/Menlo.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "C:/Windows/Fonts/consola.ttf",
    ],
}


def _font(kind: str, size: int, index: int = 0):
    from PIL import ImageFont

    for path in FONT_CANDIDATES[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size, index=index if path.endswith(".ttc") else 0)
    return ImageFont.load_default(size)


class Fonts:
    def __init__(self) -> None:
        self.body = _font("sans", 17)
        self.small = _font("sans", 14)
        self.title = _font("bold", 40, index=1)
        self.subtitle = _font("sans", 21)
        self.label = _font("bold", 15, index=1)
        self.mono = _font("mono", 14)
        self.mono_big = _font("mono", 17)


def _wrap(text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            trial = f"{line} {word}".strip()
            if font.getlength(trial) <= width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


class Block:
    """A chat element: measures itself and draws at a vertical offset."""

    def height(self) -> int:
        raise NotImplementedError

    def draw(self, d, y: int) -> None:
        raise NotImplementedError


class UserBubble(Block):
    def __init__(self, fonts: Fonts, text: str) -> None:
        self.f, self.text, self.shown = fonts, text, len(text)
        self.lines = _wrap(text, fonts.body, 520)

    def height(self) -> int:
        return 24 * len(self.lines) + 28 + 18

    def draw(self, d, y: int) -> None:
        width = max(self.f.body.getlength(line) for line in self.lines) + 32
        x = W - PAD - width
        d.rounded_rectangle([x, y, W - PAD, y + self.height() - 18], 16, fill=USER_BG)
        left = self.shown
        for i, line in enumerate(self.lines):
            d.text((x + 16, y + 13 + 24 * i), line[: max(left, 0)], font=self.f.body, fill=TEXT)
            left -= len(line) + 1


class ToolCard(Block):
    def __init__(self, fonts: Fonts, name: str, arguments: dict, rows: list) -> None:
        self.f, self.name, self.rows = fonts, name, rows
        self.args = json.dumps(arguments, separators=(", ", ": "))
        self.arg_lines = _wrap(self.args, fonts.mono, W - 2 * PAD - 40)
        self.rows_shown = 0
        self.running = True

    def height(self) -> int:
        body = 22 * self.rows_shown + (14 if self.rows_shown else 0)
        return 44 + 19 * len(self.arg_lines) + 30 + body + 18

    def draw(self, d, y: int) -> None:
        bottom = y + self.height() - 18
        d.rounded_rectangle([PAD, y, W - PAD, bottom], 12, fill=PANEL, outline=BORDER)
        d.text((PAD + 18, y + 13), "TOOL CALL", font=self.f.label, fill=TOOL)
        x = PAD + 18 + self.f.label.getlength("TOOL CALL") + 12
        d.text((x, y + 12), f"opcua · {self.name}", font=self.f.mono_big, fill=TEXT)
        status = "running…" if self.running else "returned"
        colour = MUTED if self.running else GOOD
        d.text(
            (W - PAD - 18 - self.f.small.getlength(status), y + 14),
            status,
            font=self.f.small,
            fill=colour,
        )
        ay = y + 44
        for line in self.arg_lines:
            d.text((PAD + 18, ay), line, font=self.f.mono, fill=MUTED)
            ay += 19
        ry = ay + 12
        if self.rows_shown:
            d.line([PAD + 18, ry, W - PAD - 18, ry], fill=BORDER)
            ry += 12
        for cells in self.rows[: self.rows_shown]:
            for cx, text, colour in cells:
                d.text((PAD + 18 + cx, ry), text, font=self.f.mono, fill=colour)
            ry += 22


class AssistantText(Block):
    def __init__(self, fonts: Fonts, text: str) -> None:
        self.f, self.text, self.shown = fonts, text, 0
        self.lines = _wrap(text, fonts.body, W - 2 * PAD - 40)

    def height(self) -> int:
        return 34 + 25 * len(self.lines) + 10

    def draw(self, d, y: int) -> None:
        d.text((PAD, y), "ASSISTANT", font=self.f.label, fill=ACCENT)
        left = self.shown
        for i, line in enumerate(self.lines):
            d.text((PAD, y + 30 + 25 * i), line[: max(left, 0)], font=self.f.body, fill=TEXT)
            left -= len(line) + 1


class Scene:
    HEADER = 52

    def __init__(self, fonts: Fonts) -> None:
        from PIL import Image

        self.f = fonts
        self.blocks: list[Block] = []
        self.scroll = 0.0
        self.frames: list[tuple[Image.Image, int]] = []

    def _content_height(self) -> int:
        return sum(b.height() for b in self.blocks)

    def _target_scroll(self) -> float:
        view = H - self.HEADER - 2 * 18
        return max(0, self._content_height() - view)

    def render(self):
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        y = self.HEADER + 18 - int(self.scroll)
        for block in self.blocks:
            h = block.height()
            if y + h > 0 and y < H:
                block.draw(d, y)
            y += h
        first = self.blocks[0] if self.blocks else None
        if isinstance(first, UserBubble) and self.scroll > first.height() - 18:
            # Keep the question in view once its bubble has scrolled away.
            d.rectangle([0, self.HEADER, W, self.HEADER + 34], fill=BG)
            d.line([0, self.HEADER + 34, W, self.HEADER + 34], fill=BORDER)
            d.text((PAD, self.HEADER + 8), "QUESTION", font=self.f.label, fill=MUTED)
            d.text(
                (PAD + self.f.label.getlength("QUESTION") + 12, self.HEADER + 7),
                first.text,
                font=self.f.body,
                fill=TEXT,
            )
        d.rectangle([0, 0, W, self.HEADER], fill=PANEL)
        d.line([0, self.HEADER, W, self.HEADER], fill=BORDER)
        d.ellipse([PAD, 21, PAD + 10, 31], fill=GOOD)
        d.text((PAD + 20, 16), "MCP server: opcua", font=self.f.label, fill=TEXT)
        chip = "profile=observe (read-only)  ·  simulated plant, no hardware"
        d.text((W - PAD - self.f.small.getlength(chip), 18), chip, font=self.f.small, fill=MUTED)
        return img

    def hold(self, seconds: float) -> None:
        self.frames.append((self.render(), int(seconds * 1000)))

    def tick(self) -> None:
        """One animation frame, easing the view toward the newest content."""
        target = self._target_scroll()
        self.scroll += (target - self.scroll) * 0.35
        if abs(target - self.scroll) < 1:
            self.scroll = target
        self.frames.append((self.render(), 1000 // FPS))

    def settle(self) -> None:
        while abs(self._target_scroll() - self.scroll) >= 1:
            self.tick()


def _card(fonts: Fonts, title: str, lines: list[tuple[str, object, tuple]], foot: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((PAD * 2, 150), title, font=fonts.title, fill=TEXT)
    y = 220
    for text, font, colour in lines:
        d.text((PAD * 2, y), text, font=font, fill=colour)
        y += 34 if font is fonts.subtitle else 28
    d.text((PAD * 2, H - 70), foot, font=fonts.small, fill=MUTED)
    return img


def _type(scene: Scene, block, total: int, seconds: float) -> None:
    steps = max(1, int(seconds * FPS))
    for i in range(1, steps + 1):
        block.shown = round(total * i / steps)
        scene.tick()


def _tool(scene: Scene, card: ToolCard, hold: float) -> None:
    scene.blocks.append(card)
    scene.settle()
    for _ in range(FPS):  # one second "running…"
        scene.tick()
    card.running = False
    for i in range(1, len(card.rows) + 1):
        card.rows_shown = i
        scene.tick()
        scene.tick()
    scene.settle()
    scene.hold(hold)


def _rows(f: dict) -> tuple[list, list, list]:
    browse = []
    names = f["sensors"]
    for i in range(0, len(names), 4):
        browse.append([(j * 220, f"• {n}", TEXT) for j, n in enumerate(names[i : i + 4])])
    browse.append([(0, f"{len(names)} Variable nodes under Sensors", MUTED)])
    read = [
        [
            (0, "node", MUTED),
            (130, "name", MUTED),
            (290, "value", MUTED),
            (440, "status", MUTED),
            (560, "source time (UTC)", MUTED),
        ]
    ]
    for r in f["readings"]:
        read.append(
            [
                (0, r["node_id"], MUTED),
                (130, r["name"], TEXT),
                (290, f"{r['value']:.2f}", ACCENT),
                (440, r["status"], GOOD),
                (560, r["time"], MUTED),
            ]
        )
    lo, hi = f["t_min"], f["t_max"]
    hist = [[(0, "source time (UTC)", MUTED), (200, "Temperature", MUTED), (340, "status", MUTED)]]
    for time, value, status in f["history"]:
        bar = "█" * (1 + round(12 * (value - lo) / ((hi - lo) or 1)))
        hist.append(
            [(0, time, MUTED), (200, f"{value:.2f}", ACCENT), (340, status, GOOD), (450, bar, BAR)]
        )
    return browse, read, hist


def render(args: argparse.Namespace) -> None:
    session = json.loads(args.session.read_text(encoding="utf-8"))
    f = _facts(session)
    fonts = Fonts()
    scene = Scene(fonts)

    intro = _card(
        fonts,
        "OPC UA MCP Server",
        [
            ("An AI agent reads a simulated factory: no PLC, no hardware.", fonts.subtitle, TEXT),
            ("", fonts.small, TEXT),
            ("Question → OPC UA tool calls → live sensor values → answer", fonts.subtitle, ACCENT),
        ],
        "Tool calls and values are real responses from the bundled simulator. "
        "The chat text is scripted.",
    )
    scene.frames.append((intro, 4000))

    user = UserBubble(fonts, QUESTION)
    user.shown = 0
    scene.blocks.append(user)
    _type(scene, user, len(QUESTION), 2.5)
    scene.hold(1.5)

    browse_rows, read_rows, hist_rows = _rows(f)
    calls = session["calls"]
    _tool(scene, ToolCard(fonts, calls[0]["tool"], calls[0]["arguments"], browse_rows), 4.0)
    _tool(scene, ToolCard(fonts, calls[1]["tool"], calls[1]["arguments"], read_rows), 6.0)
    _tool(scene, ToolCard(fonts, calls[2]["tool"], calls[2]["arguments"], hist_rows), 5.0)

    text = _answer(f)
    answer = AssistantText(fonts, text)
    scene.blocks.append(answer)
    _type(scene, answer, len(text), 6.0)
    scene.settle()
    scene.hold(7.0)

    outro = _card(
        fonts,
        "Try it without hardware",
        [
            ("git clone https://github.com/IndustriAgents/OPCUA-MCP.git", fonts.mono_big, TEXT),
            ("cd OPCUA-MCP && uv sync --all-packages", fonts.mono_big, TEXT),
            ("uv run --no-sync opcua-mock-server", fonts.mono_big, TEXT),
            ("", fonts.small, TEXT),
            ("Then add the bridge to your MCP client:", fonts.subtitle, MUTED),
            ("command  npx -y opcua-mcp-server", fonts.mono_big, ACCENT),
            ("env      OPCUA_SERVER_URL=" + ENDPOINT, fonts.mono_big, ACCENT),
        ],
        TRY_URL,
    )
    scene.frames.append((outro, 7000))

    images = [img.quantize(colors=64, dither=0) for img, _ in scene.frames]
    images[0].save(
        args.out,
        save_all=True,
        append_images=images[1:],
        duration=[ms for _, ms in scene.frames],
        loop=0,
        optimize=True,
        disposal=1,
    )
    total = sum(ms for _, ms in scene.frames) / 1000
    if shutil.which("gifsicle"):
        subprocess.run(["gifsicle", "-O3", "--batch", str(args.out)], check=True)
    size = args.out.stat().st_size / 1e6
    print(f"wrote {args.out.relative_to(ROOT)}: {len(images)} frames, {total:.0f} s, {size:.1f} MB")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture", help="record real tool calls against a running mock")
    cap.add_argument("--endpoint", default=ENDPOINT)
    cap.add_argument("--session", type=Path, default=SESSION)
    ren = sub.add_parser("render", help="draw the GIF from the recording")
    ren.add_argument("--session", type=Path, default=SESSION)
    ren.add_argument("--out", type=Path, default=GIF)
    args = parser.parse_args(argv)
    {"capture": capture, "render": render}[args.command](args)


if __name__ == "__main__":
    main(sys.argv[1:])
