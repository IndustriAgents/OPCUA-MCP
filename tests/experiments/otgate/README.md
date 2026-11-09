# otgate simulator experiment

This opt-in experiment follows [issue #279](https://github.com/IndustriAgents/OPCUA-MCP/issues/279#issuecomment-6750105961).
It connects otgate's real bearer-authenticated MCP HTTP channels to the Python
OPCUA-MCP worker over private stdio. The worker uses the bundled OPC UA simulator
on an ephemeral loopback port. A separate OPC UA client changes the simulator's
live state as an HMI would. The test records OPC UA Write requests at the
simulator's attribute service, including attempts that could leave a value
unchanged.

Run from the repository root on macOS. The agent probe uses `sandbox-exec`
to allow outbound connections only to the agent port. Keep otgate in a
separate environment because it uses MCP SDK 1.x and the bridge uses 2.x.

```bash
uv sync --locked --all-packages
lab_dir=$(mktemp -d)
uv venv --python 3.13 "$lab_dir/otgate"
uv pip install --python "$lab_dir/otgate/bin/python" \
  'otgate[opcua] @ git+https://github.com/alikup-ai/otgate.git@4f54555878f8f52fa20b089dc204898f41b254fb' \
  'mcp==1.30.0' 'asyncua==2.1.0' 'httpx==0.28.1'
"$lab_dir/otgate/bin/python" tests/experiments/otgate/run.py \
  --workspace-python .venv/bin/python --output-dir "$lab_dir/evidence"
```

The harness checks the initial range denial, the exact approved write and its
read-back, and sequential repeat approval. It changes a guard tag and then a
live setpoint in separate cases to check stale approval. It also checks that
the bridge blocks a node excluded by its operator policy, the agent cannot
self-approve or connect to the operator or OPC UA ports, and the bridge audit
omits the synthetic values recorded by otgate.

The evidence directory contains the Write trace, both audit files, and process
logs. The pinned otgate revision surfaces the bridge's policy refusal as a
backend error and leaves that approval pending. The bridge still sends zero OPC
UA writes in that case. The test exercises one Python worker, sequential
approvals, and local lab isolation. The unsecured loopback endpoint and
sandbox probe are specific to this experiment.
