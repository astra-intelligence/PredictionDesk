<!-- Every PR carries its spec attachment and a MANDATORY evidence block (harness pipeline
     step 5). Evidence is a CLAIM: CI re-runs the gate and the independent spec-verifier
     re-derives everything, bound to the exact head SHA below. Stale, missing, or SHA-mismatched
     evidence is rejected, not trusted. -->

OpenSpec change: `<name>` <!-- openspec/changes/<name>/ — required, T2 included -->
Tier/lane: `tier:T?` / `lane:?` <!-- must match the PR labels set at triage -->

## Evidence

### Gate (mandatory)

Head SHA this gate output was produced on (`git rev-parse HEAD` after the final commit):

```
<sha>
```

`python3 -m desk.verify gate --json` output (full, unedited):

```json
<paste>
```

### Timings
<!-- Only when the change's evidence contract asks for them (e.g. demo < 90s, gate duration). -->

### Screenshots
<!-- MANDATORY for any UI/visual claim: PR-embedded images, before/after where it applies.
     The verifier rejects UI claims without them. -->

### Backtest / lab
<!-- Lab PRs only: run dir + metrics INCLUDING the sealed holdout window. -->

## Criteria → evidence map
<!-- One line per numbered success criterion from the OpenSpec change. The verifier checks every
     row; a criterion with no covering evidence fails the PR. -->

| # | Success criterion | Evidence |
|---|---|---|
| 1 |  |  |

## Checklist

- [ ] Gate green locally on exactly the head SHA above (re-run after the final commit)
- [ ] Named eval section(s) land in this PR and fail without the change (no tautological tests)
- [ ] No edits to protected paths (.github/**, desk/eval/golden/**, desk/signals/strategy_config.json, desk/verify.py, scripts/ci/**) — or this PR is tier:T0
- [ ] No secrets, keys, tokens, or private material anywhere in diff or evidence
