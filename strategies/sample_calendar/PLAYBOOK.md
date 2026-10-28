# sample_calendar — playbook

## What it is

The minimal sample tenant: buy a token at 1/4 of its captured window, sell at 3/4, hold the rest
to settlement. No edge model, no external data, no domain vocabulary — it exists to prove the
tenant contract end-to-end (manifest discovery, config-row seal, describe hook, eval section,
paper engine fills + fees) with nothing but the capture store. Copy THIS shape for a new domain
(sports, rates, elections); `sample_weather` shows the same contract with a full external-data
pipeline on top.

## How it trades

- `taker` mode: crosses the spread with wide-limit IOC orders — guaranteed fill, pays the
  category's taker fee.
- `maker` mode: rests at the as-of best bid/ask with a GTD validity window
  (`maker_validity_s`) — zero fee, fills only if the market trades through the price.
- Universe: the richest captured tokens first, at most `n_markets` markets per fire, skipping
  tokens with fewer than `min_snaps` snapshots.

## Expectations

Expected P&L is the spread + fees, i.e. slightly negative — this is a plumbing smoke test, not a
signal. It ships enabled so a fresh desk visibly trades on captured books from the first fire;
pause it via the config row (or the dashboard pause/resume control) when you no longer need it.

## Params

| param | default | meaning |
|---|---|---|
| `qty` | 50.0 | shares per clip |
| `n_markets` | 3 | max markets per fire |
| `min_snaps` | 2 | minimum captured snapshots to consider a token |
| `mode` | `taker` | `taker` (cross) or `maker` (rest) |
| `maker_validity_s` | 3600 | maker GTD validity window (seconds) |
