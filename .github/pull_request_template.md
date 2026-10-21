# Which bounty?

How to claim, what CI does, closure and payout rules: the `README.md` in the
room's folder (`rooms/<ROOM>/`, the 43-character room ID).

Payout: bc1q…

## Checklist

- [ ] Only your submission in `rooms/<ROOM>/submissions/` changed:
      `<username>.00.jsonl.b64` (more `.NN` parts only if it exceeds 50,000
      lines)
- [ ] `<username>` is unique and a valid file name: your GitHub username, or, if
      filename-safe, your Matrix ID with `:` changed to `+` (`@alice:matrix.org`
      becomes `@alice+matrix.org`; IPv6 server names or IDs with `/` are
      unsupported, so use your GitHub username). A later claim adds `-2`
      (`alice-2.00.jsonl.b64`)
- [ ] No plaintext `.jsonl` committed (only base-64 encoded)
- [ ] The `Payout:` line above has your Bitcoin address (no address, no payout)
- [ ] Events are for this room only; anything unresolvable has evidence attached
