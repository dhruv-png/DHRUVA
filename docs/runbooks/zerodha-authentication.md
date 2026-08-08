# Runbook — Zerodha authentication

**What this covers.** Enrolling your Kite Connect application credentials,
performing the manual browser login, and checking where you stand. Nothing here
places an order, syncs holdings or positions, or fetches market data.

**What it does not do.** Authenticating is not the same as having data. After a
successful login the instrument master and daily bars still have no wired
ingestion path; that is a separate, separately approved slice.

---

## Never paste a secret anywhere it can be kept

Your Kite **API secret**, **request token**, **access token**, Zerodha
**password**, **PIN** and **TOTP** must never be typed or pasted into:

- Claude, ChatGPT or any other assistant — including this project's own sessions;
- a GitHub issue, pull request, commit message or code comment;
- a screenshot or a screen share;
- documentation, including this file;
- a shell command argument, a `.env` file, or anything under version control.

The commands below are built so that none of this is *possible* by accident.
There is no `--api-secret`, no `--request-token` and no `--access-token` option:
every sensitive value is typed at a hidden prompt that echoes nothing, and a
test reads the argument parser and fails the build if one is ever added. Piping
a value in (`echo … | dhruva-broker …`) is refused too, because a pipeline that
worked would end up in a script with the secret on the line above it.

Every placeholder in this document is visibly fake.

---

## 1. Create the Kite Connect app

1. Sign in at <https://developers.kite.trade/login> and create an app.
2. Note the **API key** and **API secret**. Keep them where you already keep
   credentials — a password manager, not a text file.
3. Set the **redirect URL**. DHRUVA does not run a web server, so this only has
   to be an address you can read the query string from. Something local such as
   `http://127.0.0.1:8000/kite/redirect` is fine: the browser will fail to
   connect and the address bar will still show the token, which is all you need.

Your trading account must have TOTP-based 2FA enabled — Zerodha requires it for
Kite Connect. DHRUVA never automates it, and never stores it.

## 2. Configure the vault master key

DHRUVA seals broker credentials with envelope encryption (ADR-070). The master
key comes from **process configuration**, never from a command argument and
never from the database:

```powershell
# 32 bytes of key material, base64-encoded. Generate once, keep it safe.
$env:DHRUVA_CRYPTO__MASTER_KEY = "<base64 of 32 random bytes>"
```

Generate it locally and store it in your password manager. Set it in your shell
profile or your environment, not in a committed file — `.env` is git-ignored and
a repository-level test fails if one appears in the tree.

**If you lose this key, every stored credential is unrecoverable.** That is by
design: the key that opens the vault is not in the vault. Losing it costs one
re-enrolment and one login, not a security incident.

The default value is a deliberate non-key placeholder. Any deployed environment
refuses it, and the cipher refuses it the moment something tries to encrypt.

## 3. Enrol the application credential

```powershell
dhruva-broker zerodha enrol --account owner-family
```

You will be asked for the API key and then the API secret. Neither is echoed;
neither reaches your PowerShell history. Both are sealed into a single
`ENROLMENT` credential — they are issued together and rotated together, so they
are one lifecycle.

`owner-family` is a **DHRUVA account label**, not your Zerodha client ID. It is
the same value you pass to `dhruva-reference`, `dhruva-news`, `dhruva-digest`
and `dhruva-export`; a mismatch produces empty results rather than an error.

Running this again **rotates** the credential. Your broker session is not
touched.

## 4. Log in

```powershell
dhruva-broker zerodha login --account owner-family
```

The command prints the official Kite login URL. Then:

1. Open it in your own browser and sign in to Zerodha as you normally would —
   including 2FA. DHRUVA never sees your password, PIN or TOTP, and does not
   open a browser for you.
2. Zerodha redirects to the URL you registered. Even if the page fails to load,
   the address bar contains `?request_token=…`.
3. Copy the value of `request_token` and paste it at the hidden prompt.

The token is valid for a few minutes and can be used once. DHRUVA exchanges it
for an access token, seals that as the `SESSION` credential, and prints a
summary that includes the broker user id and the expiry — **never the token**.

Your enrolled application credential is untouched by this.

## 5. Verify — without placing an order

```powershell
dhruva-broker zerodha status --account owner-family
```

This is the whole verification. It reports one of:

| State | Meaning | What to do |
|---|---|---|
| `ENROLMENT_MISSING` | Nothing enrolled | Step 3 |
| `SESSION_MISSING` | Enrolled, never logged in | Step 4 |
| `SESSION_EXPIRED` | Logged in, but the session has ended | Step 4 again |
| `SUCCESS` | Enrolled with a live session | Nothing |

On `SUCCESS` it also prints the **broker user id**, which is the one fact that
confirms you authenticated as the account you meant to. Check it.

There is deliberately no "test connection" that calls the broker. A read-only
call would still be a call, and the state above answers the operator's real
question — *can I authenticate, and if not, why not* — without one. Exit status
is `0` for `SUCCESS` and `3` otherwise, so a script can branch on it.

## 6. Session expiry, and re-logging in

Kite access tokens **expire at 06:00 IST the morning after they are issued** —
a regulatory requirement, not a configuration. A session created at 15:30 today
dies at 06:00 tomorrow; one created at 03:00 dies at 06:00 the *same* morning,
three hours later.

So a login is a daily act. Repeat step 4. It replaces only the `SESSION`
credential; the enrolment credential's rotation history is not disturbed, which
is why "when did I last rotate my API secret" stays a meaningful question.

A master logout from Kite Web also invalidates the token early. The status
command will still report `SUCCESS` until the recorded expiry passes, because
DHRUVA cannot see that logout without making a call. If broker calls fail while
status says `SUCCESS`, log in again.

## 7. Rotating the application secret

Regenerate the secret in the Kite developer console, then repeat step 3. The
command reports `Rotated` rather than `Stored`.

Your existing session keeps working until it expires — the access token was
already issued and does not depend on the secret afterwards. The next login uses
the new secret.

## 8. Revoking

- **End the DHRUVA session:** there is no `logout` subcommand in this slice.
  Delete the `SESSION` row, or simply wait: the token expires at 06:00 IST and
  cannot be renewed without a fresh browser login.
- **Revoke everything:** regenerate or delete the app in the Kite developer
  console. The enrolled secret stops working immediately, and no further login
  is possible. Then re-enrol (step 3) if you intend to continue.
- **Rotate the vault master key:** every stored credential must be re-sealed.
  Until that job exists, the supported route is to set a new key and re-run
  steps 3 and 4 — which costs one enrolment and one login.

---

## What is stored, and where

Two rows in `credential`, both for `account = owner-family`, `broker = zerodha`
(ADR-077):

| Purpose | Holds | Rotated |
|---|---|---|
| `ENROLMENT` | API key and API secret | When you choose |
| `SESSION` | Access token, broker user id, issued/expiry instants | Every login |

Both are AES-256-GCM ciphertext with a per-record wrapped data key. The
ciphertext is bound to `(credential_id, account_id, broker, purpose)`, so a row
copied between accounts, brokers, purposes or records fails to decrypt rather
than opening as something it is not.

Of the fifteen fields Kite returns from the session exchange, three are stored.
Your email, avatar URL, enabled exchanges, permitted order types, `public_token`
and `enctoken` are read by nothing and kept by nothing.

## Troubleshooting

**"the broker rejected the login"** — the request token was already used, has
expired (they last minutes), or was mistyped; or the enrolled API secret is
wrong. Try step 4 again with a fresh token first; only re-enrol if that fails
twice.

**"this terminal cannot hide what you type"** — you are running in a terminal
that cannot suppress echo, or output is redirected. Run the command in an
ordinary interactive terminal. It refuses rather than echoing, on purpose.

**"secrets are only accepted from an interactive terminal"** — stdin is a pipe
or a file. Type the value; do not pipe it.

**"credential vault master key is still a development placeholder"** — set
`DHRUVA_CRYPTO__MASTER_KEY` (step 2).

When reporting a problem, paste the error text and the `status` output. Neither
contains a secret — that is tested. Do not paste anything else.
