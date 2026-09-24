# Architecture decision records

A decision record is written when a choice constrains more than the change that
makes it: a support promise, a dependency strategy, a boundary other work has to
respect. Most of the reasoning in this repository lives beside the code, in
comments and in [architecture.md](../architecture.md), and should stay there.
An ADR is for the few decisions that later issues have to be measured against.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-two-first-class-runtimes.md) | Two first-class runtimes | Accepted |

## Writing one

1. Copy [template.md](template.md) to `NNNN-short-title.md`, numbered after the
   highest one above.
2. Open it as **Proposed** in a pull request, linked from the issue that asked
   for the decision. The discussion happens on the PR.
3. Merge it as **Accepted**, and add a row to the table above in the same PR.

An accepted ADR is not edited to change its decision. A new ADR that reverses or
replaces it says so in its header (`Supersedes: 0001`), and the old one's status
becomes **Superseded by NNNN** — that one-line edit, and corrections of fact or
broken links, are the only changes made to an accepted record.
