# linux-idmap provider adapter fixtures

This directory contains reproducible patches for using `linux-idmap` in NMRPeak
and NMRNet. It is separate from the installable package: provider configuration,
image selection and permission policy belong to the consumer repositories.

| Consumer | Adapter |
|---|---|
| NMRPeak | Calls `linux_idmap.probe.probe_identity` from Python and translates the returned host maps for its existing ACL decisions. |
| NMRNet | Calls `linux-idmap-probe` from its shell deployment code and consumes the ordered numeric output before selecting permission policy. |

Both adapters probe an image before the workload process exists. The scientific
runtimes are unchanged. NMRNet's separate inverse mapping in `tools/docker.sh`
is also unchanged.

## Reproduce a consumer tree

`consumers.json` records the repository, exact base commit, patch filename,
SHA-256 digest and resulting Git tree for each fixture. Select its entry and
verify the patch digest before applying it. Use a disposable detached worktree
at that entry's base:

```sh
# Replace these values with paths and the selected manifest entry.
set -eu
repository=/absolute/path/to/provider-repository
review=/absolute/path/to/clean-review
patch=/absolute/path/to/CONSUMER.patch
base=BASE_COMMIT
expected_sha256=PATCH_SHA256
expected_tree=RESULT_TREE

printf '%s  %s\n' "$expected_sha256" "$patch" | sha256sum --check -
git -C "$repository" worktree add --detach "$review" "$base"
git -C "$review" apply --check "$patch"
git -C "$review" apply --index "$patch"
test "$(git -C "$review" write-tree)" = "$expected_tree"
```

The checks require both the patch digest and resulting tree to match the
manifest. Keep the original consumer checkout unchanged while examining or
testing the reconstructed tree.

## Install and test the adapter

These fixtures use `linux-idmap` **0.3.0a5**. Build its wheel using the
[package instructions](../../packages/linux-idmap/README.md), then install that
wheel in the selected host Python 3.11+ environment. NMRPeak uses that environment's
Python; NMRNet requires its `linux-idmap-probe` command on `PATH`.

From the reconstructed consumer worktree, run:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
  /path/to/host-python -m unittest discover -s tests/host -v
```

The NMRPeak tests exercise its actual deployment module with a substituted
managed-probe effect. The NMRNet tests exercise its shell adapter, signal traps
and the real CLI with a synthetic probe effect. They cover request propagation,
translation, refusal and diagnostic handling at the consumer boundary.

The package's [API and CLI reference](../../packages/linux-idmap/REFERENCE.md)
defines request policy, confinement, output and resource-state handling.
