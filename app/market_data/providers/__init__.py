"""Provider adapters.

One module per provider. Each translates a provider's wire format into
`app.market_data.contracts` types and satisfies the protocols in
`app.market_data.protocols`. No module here is imported by the behavioural
engine; the engine receives an already-constructed provider.

Status (2026-10-19): both adapters are unimplemented. Their modules carry the
documented API facts gathered during M3 planning so the implementation does not
start from a guess, and they raise `CredentialsMissing` rather than returning
anything that could be mistaken for market data.
"""
