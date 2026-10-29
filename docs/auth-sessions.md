# Login backoff and browser sessions

Local dashboard logins and opt-in external bridge cookies issued by 0.3.0 have
a random 256-bit session ID. The database stores its SHA-256 digest, account
identity, authentication source, optional host subject, creation/expiry time
and revocation time. Two logins within the same second create distinct
sessions. Signing out revokes the current ID before removing the cookie; a
stolen copy of that cookie then fails on every API instance sharing the DB.
The account password fingerprint and live role checks still apply to local
sessions. Password changes and deleted accounts retain their previous
immediate sign-out behavior.

An unavailable session database fails authentication rather than accepting
an unchecked session. Bridge sessions use their configured short lifetime;
revocation only removes that bridge session. A still-valid host login remains
authoritative, including its current role and explicit access refusal. Manage
the host login through the host application's own session controls.

`GET /api/auth/sessions` lists up to the 100 most recent active sessions owned
by the signed-in identity, with public hashed `session_id`, creation/expiry
timestamps and `current`. `POST /api/auth/sessions/{session_id}/revoke` revokes
one owned session and records an audit entry. All authenticated roles may
manage their own sessions. A different username, source or host subject gets
404 rather than information about another identity's session. Revoking the
current session ends access on the next request. These endpoints can be used
by a host app's account-security screen.

## Upgrade compatibility

Previously issued cookies, and cookies manually generated with
`auth.sessions.sign_session` without a `session_id`, remain stateless until
their existing maximum age expires. They still require the correct signature
and password fingerprint. They cannot be listed or individually revoked by
the new session store. Rotate `secret_key` on upgrade to invalidate all old
cookies immediately, or reset an individual local account's password to end
its old cookies. HTTP login and bridge issuance always create tracked
sessions; production clients should use those endpoints rather than signing
cookies manually.

## Shared login backoff

Every local password attempt first reserves capacity in database-backed
account and direct-client-peer budgets. All API processes sharing the same
database and `secret_key` therefore enforce the same backoff. There is no
per-process fallback. Attempts for unknown accounts take the same password
verification path and use the same budgets as known accounts.

The first five attempts are admitted by default. Beginning with the next
attempt, a recent failure sequence produces HTTP 429 with a `Retry-After`
header. The backoff starts at one second and doubles with additional admitted
failures up to 300 seconds. Waiting through a delay allows another attempt;
it does not reset the failure counter. One hour of inactivity resets the
budget. Rejected attempts do not perpetually extend the deadline.

A successful authentication clears the account budget and removes only its
own reservation from the peer budget, preserving that peer's failures
against other accounts. This avoids penalizing ordinary repeated successful
logins while preventing them from clearing unrelated failed guesses.

```python
CeleryManagerConfig(
    ...,
    login_backoff_threshold=5,
    login_backoff_max_seconds=300,
    login_backoff_reset_seconds=3600,
)
```

All three settings must be positive integers. Peer identity uses
`request.client.host`; untrusted `X-Forwarded-For` is never interpreted by
this feature. Deploying behind a proxy can make clients share a budget unless
your ASGI server is configured to trust only the intended proxy and exposes
the original client correctly. Account backoff can also be triggered by
someone deliberately guessing a victim's account; it expires and does not
permanently lock the account. Host/external login flows retain their host's
own admission policies; disabling local login refuses password attempts
before checking credentials or reserving a budget.

Expired browser sessions and stale login budgets are removed opportunistically
on login/bridge issuance, in batches of at most 1000 rows from each indexed
table. Revoked rows remain until their original expiry, allowing investigation.
An idle deployment retains expired rows until subsequent sign-in activity;
this is not a separate periodic background purge.
