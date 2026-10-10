# Kata node configuration

Three node-level settings are required: two in the Kata configuration (sections 1 and 2) and one
in containerd's CRI configuration (section 3). All three were established by load-testing the
plugin. Skipping 1 or 2 causes VM out-of-memory kills under real load; skipping 3
passes every host device into the VM and voids the isolation claim.

## 1. `default_memory = 512`

Kata's guest memory is the Pod's limits plus `default_memory` (the VM's fixed floor). 512 MiB
keeps the guest at about 2 GiB for the default container limits while leaving enough room for the
guest kernel, page cache and `dockerd`. The RuntimeClass `overhead.podFixed.memory` (1024Mi) is
sized from it (512 MiB base + 512 MiB host-side headroom): **keep them in step**.

Evidence: peak in-guest need was 703 MiB; a 3.5 h load test peaked at 1474 of 2560 MiB.

## 2. `block_device_cache_set = true` and `block_device_cache_direct = true`

The Docker disk is a raw block device attached to the VM. With the default caching the *host*
caches the disk's pages too, on top of the guest's own page cache ("double caching"). Under a
write-heavy Docker workload the host-side page cache is charged to the Pod's memory cgroup and
the **whole VM was OOM-killed** in a long-running load test. With these two options the VMM opens the disk
with `O_DIRECT` (verified: fd flags `02140002`) and the host caches nothing: the same load test passed
(6/6 checks, 3.5 h) with a cgroup peak of 1474 / 2560 MiB.

```toml
# in the [hypervisor.clh] section of the Kata configuration
default_memory = 512
block_device_cache_set = true
block_device_cache_direct = true
```

## 3. containerd: `privileged_without_host_devices` (REQUIRED for the isolation claim)

On the `kata` runtime in containerd's CRI configuration:

```toml
privileged_without_host_devices = true
privileged_without_host_devices_all_devices_allowed = true
```

- `privileged_without_host_devices = true`: without it, containerd passes **every host device node**
  into the VM for the privileged `dind` container. Agents could then read the host's disks from
  inside the sandbox, defeating the point of the VM boundary.
- `privileged_without_host_devices_all_devices_allowed = true`: a privileged `dockerd` needs the
  loop and overlay devices *inside the guest*; with host devices withheld, this lets the guest's own
  devices through.

## Applying it

Settings 1 and 2 go in the Kata `configuration.toml` for the runtime you register; setting 3 goes
in containerd's CRI runtime entry for `kata`. Do not edit the Kata file shipped inside the release;
use a copy or a drop-in so upgrades do not silently revert it.

A change to the CRI configuration or to the Kata configuration needs containerd to restart (on
Talos, a node reboot). It then applies to newly started Pods.

### Talos (Kata from a system extension)

1. Copy the extension's `configuration.toml` (Cloud Hypervisor build) to a persistent path under
   `/var`, for example `/var/lib/kata-config/configuration.toml` (via a machine-config `files:`
   entry), and make the Kata edits above in the copy.
2. Point containerd's Kata runtime at the copy and set the device flags, using a CRI customization
   part (`/etc/cri/conf.d/20-customization.part`):

   ```toml
   [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata]
     privileged_without_host_devices = true
     privileged_without_host_devices_all_devices_allowed = true
     [plugins."io.containerd.cri.v1.runtime".containerd.runtimes.kata.options]
       ConfigPath = "/var/lib/kata-config/configuration.toml"
   ```
3. Apply the machine config and reboot the node.

### kata-deploy (verify against your Kata version)

Written from the documented layout, not tested. The tested setup was Kata 3.28.0 with
Cloud Hypervisor on Talos. kata-deploy installs configurations under
`/opt/kata/share/defaults/kata-containers/` and supports drop-in fragments in a `config.d/`
directory next to the configuration file: put the Kata edits in a drop-in on every Kata node and
confirm they took effect. kata-deploy also writes the containerd runtime entry: **verify both
`privileged_without_host_devices` lines are present in the resulting containerd configuration**
(`containerd config dump | grep -A3 runtimes.kata`) and add them if not.

## Re-copy on every Kata upgrade

A copied configuration does not follow upstream changes. After each Kata upgrade, diff the new
shipped configuration against your copy, carry across new options, and confirm the Kata values
above are still set.

## Verify on a node

With a sandbox Pod running, find the VMM process (`cloud-hypervisor`) and its block-device fd, then
read the flags:

```sh
ls -l /proc/<vmm pid>/fd            # find the fd that points at the Docker disk block device
grep flags /proc/<vmm pid>/fdinfo/<fd>
```

`O_DIRECT` (octal 040000) must be set in `flags`; a working node shows `02140002`.
