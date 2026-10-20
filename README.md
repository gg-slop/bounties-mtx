# Matrix room bounty hunts (missing prevs/auths)

## Bootstrapping

This repository collects Matrix event DAGs and bounty claims. To inspect a
room's current DAG, follow the setup steps below. Encode claim submissions as
`.jsonl.b64` files, and follow
[the pull request template](.github/pull_request_template.md) when opening a
claim PR.

```shell
git clone https://github.com/gamesguru/bounties-mtx.git
cd bounties-mtx
git submodule update --init --recursive
python3 ./scripts/bounty.py decode
# ~~~~
# you need rezzy for this, otherwise use your own tools/scripts
cd res/L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI/
# run state res on the current DAG
rezzy --debug -i merged-L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI-v12-d1-71756.jsonl
# print missing event IDs
rezzy inspect gaps -i merged-L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI-v12-d1-71756.jsonl
# print detailed report on missing/gaps
rezzy inspect gaps -i \
  merged-L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI-v12-d1-71756.jsonl --json
```
