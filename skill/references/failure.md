# When something fails

Load on any tool error, malformed result, exhausted retry, or suspected loop.

## First, roll back

Discard whatever the failed operation put into context. Partial output from a failed step is one of the more effective ways to produce a confidently wrong answer three turns later.

Then add exactly one line:

```
[tool:jira.search failed — invalid field "assigneeName"; expected "assignee". details: err:7c21a9]
```

Error class, cause, suggested fix. The stack trace, the raw payload and the full response go to the archive under that reference, and come back only if diagnosis actually needs them.

## Then classify

| Class | Meaning | Do |
|---|---|---|
| **Deterministic** | Bad argument, wrong field, schema violation, missing required parameter | Fix the request yourself. No model call needed. |
| **Transient** | Timeout, rate limit, 5xx | Back off exponentially, retry within the budget |
| **Capability** | The model got it wrong, or the task was harder than assumed | Climb the ladder below |
| **Fatal** | Not authorised, resource gone, operation impossible | Stop. Report what happened and why. |

## The escalation ladder

```
0  Fix it deterministically, no LLM
1  Retry with changed input          — never the same call twice
2  Raise reasoning effort, same model — the cheapest real capability increase
3  Escalate model tier                — recompute budgets for the new tokenizer
4  Fall back to an alternative tool
5  Abort with a normalized error
```

Rung 2 is the one most often skipped. Between "retry identically" and "call the expensive model" there is a cheaper option that frequently works.

**Never re-ask for the same operation without changing something relevant.** If the input is identical, the result will be too — you are just paying twice.

## Loop protection

Stop and escalate when you notice:

- The same tool called with the same arguments more than twice.
- A repeating cycle: search → summarise → search → summarise.
- Context growing turn over turn without the task advancing.
- Retry, tool-call, sub-agent, iteration, token or wall-clock limits approaching.

A loop that has run three times will not resolve on the fourth. Change approach or hand back what you have with an honest statement of where it stopped.

## Circuit breakers

A tool that has failed repeatedly in a short window is treated as unavailable until it recovers. Stop calling it, use the fallback, and say so in the answer rather than retrying into a wall.

## What to tell the user

What was attempted, what failed, what you did instead, and what is missing from the answer as a result. Never present a partial answer as complete.
