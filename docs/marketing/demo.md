# Record a 60-second OPC UA MCP demo

**Working title:** Ask AI about OPC UA data — no PLC required

**Status:** recording script prepared. No video or GIF has been recorded or
verified as part of this change. This is a 60-second tour after setup, not a
claim that installing every prerequisite takes 60 seconds.

Use the mock plant for sensor/history/subscription shots and the separate alarm
mock for retained alarms. Record the real tool calls and returned data. Never
replace returned values with scripted numbers.

## Setup before recording

Required: Git, uv, Python 3.10+, Node 22.13+, npm, and a configured Claude Code
installation with access to an AI model. Keep the mock servers on an isolated
development machine; do not use production endpoints.

In a checkout of this repository, prepare the plant:

```bash
uv sync --all-packages
uv run --no-sync opcua-mock-server --endpoint opc.tcp://127.0.0.1:4840/freeopcua/server/
```

Leave it running. In a second terminal, from the repository root:

```bash
npm --prefix packages/mock-server-alarms ci
npm --prefix packages/mock-server-alarms start
```

This separate server exposes `opc.tcp://localhost:4842/UA/Alarms`.
Its Temperature node starts at 100 and its high limit is 80, so a retained
alarm is available without changing a value.

In a third terminal, from the repository root, register two distinct connections:

```bash
claude mcp add opcua-plant-demo -e OPCUA_SERVER_URL=opc.tcp://127.0.0.1:4840/freeopcua/server/ -- npx -y opcua-mcp-server
claude mcp add opcua-alarm-demo -e OPCUA_SERVER_URL=opc.tcp://localhost:4842/UA/Alarms -- npx -y opcua-mcp-server
claude
```

Record the resolved package version with `npm view opcua-mcp-server version`.
If the demonstration needs unreleased features, build the branch and configure
its local entry point instead; label the video with the commit being shown.

Let the plant accumulate a little history, and verify both connections before
recording. Start a fresh conversation and keep the tool-call details visible.
Use a 1920×1080 recording with readable text; crop out unrelated windows and
account details. Keep a copy of the unedited capture.

## Shot list and narration

Timing is an editing target. Allow real tool calls to finish, and label any
speed-up or jump cut.

| Time | Screen action | Voice-over |
|---|---|---|
| 0–6 s | Show the repository title and running local mock | “Explore industrial data with an AI assistant. This demonstration uses a simulated OPC UA plant.” |
| 6–14 s | Show the client connection and endpoint | “OPC UA MCP connects your assistant to an OPC UA server. You can try it without a PLC.” |
| 14–24 s | Ask for the main mock temperature; expand the tool result | “Ask for a sensor reading, and inspect the value returned by the tool.” |
| 24–36 s | Subscribe to temperature, then ask for buffered changes after a short wait | “Subscriptions collect changes. Ask again to retrieve the buffered readings.” |
| 36–45 s | Read the latest three temperature history records | “Read recent history when the connected server supports it.” |
| 45–54 s | Switch visibly to the separate alarms connection and list active alarms | “This second mock supports retained alarms. Here is its active high-temperature condition.” |
| 54–60 s | Show repository URL and the mock quick start | “Available in Python and Node. Try the mock, then share your compatibility results.” |

On-screen labels:
- “Local simulation — no physical equipment”
- “Separate Alarms & Conditions mock” during the alarm shot
- “Writes and methods available; configure permissions before equipment access”
- “github.com/midhunxavier/OPCUA-MCP” on the closing frame

## Exact prompts

Send these one at a time. Client wording and presentation can vary; the evidence
is the actual tool output.

1. “Use opcua-plant-demo to read node ns=2;i=3, the mock temperature. Show the value
   returned by the tool. Do not write any values or invoke methods.”
2. “Use opcua-plant-demo to subscribe to ns=2;i=3 with publishing_interval 500.”
3. After several seconds: “Use opcua-plant-demo to list subscriptions and show the
   buffered changes. Use only values actually returned by the tool.”
4. “Use opcua-plant-demo to read the latest three historical values for ns=2;i=3.”
5. “Use opcua-alarm-demo to list the retained active alarms. Show the condition
   name, source, severity, active state, and acknowledgement state.”
6. “Cancel the data subscription using its returned subscription ID.”

Do not ask the main plant mock for retained alarms: it emits plain events and
does not implement the condition model. Avoid alarm acknowledgement in the
first overview because it changes server state.

## Export the video and README GIF

Export the edited 60-second capture as H.264 MP4, for example
`docs/assets/opcua-mcp-demo.mp4`. Create a silent GIF from a real 10-second
sensor/subscription section. Adjust the start time after reviewing the edit.

With FFmpeg installed, from the repository root:

```bash
ffmpeg -ss 14 -t 10 -i docs/assets/opcua-mcp-demo.mp4 \
  -vf "fps=8,scale=800:-1:flags=lanczos,palettegen" -frames:v 1 demo-palette.png
ffmpeg -ss 14 -t 10 -i docs/assets/opcua-mcp-demo.mp4 -i demo-palette.png \
  -lavfi "fps=8,scale=800:-1:flags=lanczos[x];[x][1:v]paletteuse" \
  -loop 0 docs/assets/opcua-mcp-demo.gif
```

Review the MP4 and GIF at README display size. Check text legibility, truthful
values, endpoint labels, and absence of secrets. Aim for a GIF under 5 MB; shorten
the clip or lower the frame rate if necessary. Do not commit the palette image.

After real files exist and have been reviewed, replace the README screenshot with:

```markdown
[![Live OPC UA mock demonstration](docs/assets/opcua-mcp-demo.gif)](https://github.com/midhunxavier/OPCUA-MCP/blob/main/docs/assets/opcua-mcp-demo.mp4)
```

If hosting the video elsewhere, use that verified video URL as the link target.
Do not add a broken placeholder link or call a still image a recording.

## Publishing copy

Title: **Ask AI about OPC UA data — no PLC required**

Description: **A local demonstration of OPC UA MCP: sensor readings, data-change
subscriptions, recent history, and a separate Alarms & Conditions mock. Python
and Node implementations are available. Start with the mock and read the
security guidance before connecting equipment.**

Link: https://github.com/midhunxavier/OPCUA-MCP

After recording, add the date, tested package version or commit, client version,
and relevant setup notes to the description. The terminal registration commands
are setup instructions, not evidence of a successful recording.
