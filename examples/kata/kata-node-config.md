# Kata node configuration

Two node-level settings are required. Both were established by the spike that preceded this
plugin; skipping either causes VM out-of-memory kills under real load.

## 1. `default_memory = 512`

Kata's guest memory is the Pod's limits plus `default_memory` (the VM's fixed floor). 512 MiB
keeps the guest at about 2 GiB for the default container limits while leaving enough room for the
guest kernel, page cache and `dockerd`. The RuntimeClass `overhead.podFixed.memory` (1024Mi) is
sized from it (512 MiB base + 512 MiB host-side headroom): **keep them in step**.

Evidence: peak in-guest need was 703 MiB; the 3.5 h soak peaked at 1474 of 2560 MiB.

## 2. `block_device_cache_set = true` and `block_device_cache_direct = true`

The Docker disk is a raw block device attached to the VM. With the default caching the *host*
caches the disk's pages too, on top of the guest's own page cache ("double caching"). Under a
write-heavy Docker workload the host-side page cache is charged to the Pod's memory cgroup and
the **whole VM was OOM-killed** (spike soak 1). With these two options the VMM opens the disk
with `O_DIRECT` (verified: fd flags `02140002`) and the host caches nothing: soak 2 passed
(6/6 checks, 3.5 h) with a cgroup peak of 1474 / 2560 MiB.

```toml
# in the [hypervisor.clh] section of the Kata configuration
default_memory = 512
block_device_cache_set = true
block_device_cache_direct = true
```

## Applying it

Both options go in the Kata `configuration.toml` for the runtime you register. Do not edit the
file shipped inside the Kata release; use a copy or a drop-in so upgrades do not silently revert it.

### Talos (Kata from a system extension)

1. Copy the extension's configuration (for Cloud Hypervisor, `configuration-clh.toml`) to a
   persistent path under `/var`, for example `/var/lib/kata-config/configuration-clh.toml`
   (via a machine-config `files:` entry), and make the two edits above in the copy.
2. Point containerd's Kata runtime at the copy with `ConfigPath` in a containerd config patch:

   ```toml
   [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata]
     runtime_type = "io.containerd.kata.v2"
     [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata.options]
       ConfigPath = "/var/lib/kata-config/configuration-clh.toml"
   ```
3. Apply the machine config; the setting takes effect for newly started Pods.

### kata-deploy

kata-deploy installs the configuration under `/opt/kata/share/defaults/kata-containers/` and
supports drop-in fragments in a `config.d/` directory next to the configuration file. Put the
three lines above in a drop-in (for example `99-hermes-sandbox.toml`, under the `[hypervisor.clh]`
table) on every Kata node, and select the Cloud Hypervisor shim (`kata-clh`) as the `handler` of
the RuntimeClass if you do not alias it to `kata`.

## Re-copy on every Kata upgrade

A copied configuration does not follow upstream changes. After each Kata upgrade, diff the new
shipped configuration against your copy, carry across new options, and confirm the three values
are still set. Verify on a node with a running sandbox Pod:
`ls -l /proc/<vmm pid>/fdinfo` on the disk fd should show flags `02140002` (`O_DIRECT` set).
