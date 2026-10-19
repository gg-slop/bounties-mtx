# Matrix room bounty hunts (missing prevs/auths)

## Bootstrapping

To set up this repo, probe the "merged" or aggregate DAG so far in question, run
the following steps. Always encode your submission in `.jsonl.b64` format. See
the PR template markdown file for details on how.

```shell
git clone https://github.com/gamesguru/bounties-mtx.git
cd bounties-mtx
git submodule update --init --recursive
python3 ./scripts/bounty.py decode
# ~~~~
# you need rezzy for this, otherwise use your own tools/scripts
cd res/
# run state res on the current DAG
rezzy --debug -i merged-L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI-v12-d1-71756.jsonl
# print missing event IDs
rezzy inspect gaps -i merged-L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI-v12-d1-71756.jsonl
# print detailed report on missing/gaps
rezzy inspect gaps -i merged-L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI-v12-d1-71756.jsonl --json
```
