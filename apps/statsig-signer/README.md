# Local Statsig signer

Internal-only signer used by `grok2api`. It accepts the existing signer JSON
contract at `POST /sign` and returns a request-bound `x-statsig-id` without
calling the unavailable external signer.

The algorithm and the current matched seed/HEX pair were derived from
`aurora-develop/grok2api` commit
`7eb88a9d31f6acc5f073ad4c0cc5ed181862f117` (`internal/grok/statsig/pure.go`).
The implementation here is intentionally standalone and standard-library-only.
