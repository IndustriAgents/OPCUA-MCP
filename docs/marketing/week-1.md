# Week 1 launch preparation

## Delivery status

| Item | State |
|---|---|
| README opening and corrected runtime requirement | Prepared in this change |
| Current roadmap | Prepared in ROADMAP.md |
| Evidence-based compatibility matrix and report template | Prepared in this change |
| MCP Registry metadata | Prepared; needs a new npm release before registry publication |
| Demo video and GIF | Recording kit prepared; no recording produced in this environment |
| GitHub repository topics | Command prepared; live topics still need applying |

## GitHub discoverability

The repository had no topics when inspected on 2026-09-17.
Add the following without removing any topics added by someone else:

```bash
gh repo edit midhunxavier/OPCUA-MCP --add-topic opcua,opc-ua,mcp,model-context-protocol,industrial-automation,iiot,scada,plc,claude,cursor
gh api repos/midhunxavier/OPCUA-MCP/topics --jq '.names'
```

Alternatively, use the gear beside **About** on the repository page and paste
the same topics. Repository topics are GitHub settings: committing keywords in
package.json does not update them.

## Recording handoff

Follow [demo.md](demo.md). It includes a 60-second screen-recording script,
an automated terminal capture option, and GIF conversion commands. Capture
against local mocks. Do not present scripted terminal output as an AI conversation.

## Before public launch

- Finish the real recording and review the outputs before embedding them.
- Apply and verify the topics.
- Have a new user try the mock guide and report where they get stuck.
- Use the [registry guide](../mcp-registry.md) for the subsequent publication step.
- Capture weekly baselines: repository visitors/clones, release asset downloads,
  package downloads, and confirmed successful evaluations. Downloads are not
  a count of unique users.

Week 1 prepares the launch. Community posts and package/registry publication
are separate week 2 activities.
