"""sample_calendar — the MINIMAL sample tenant: zero external data, zero domain knowledge.

It proves the full plugin contract (manifest discovery, config-row seals, hooks, eval sections)
with nothing but the capture store: buy a token early in its captured window, sell late. No edge
is claimed and no external API is touched — if your strategy's domain is sports, rates, or
anything else, THIS is the shape to copy; sample_weather is the worked example of a full
external-data pipeline on top of the same contract.
"""
