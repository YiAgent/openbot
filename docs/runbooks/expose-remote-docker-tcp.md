# Expose Docker daemon on `wy-linux` over TCP (LAN-only)

**Purpose**: fallback transport for the vendored SWT-bench harness when
SSH multiplex / keepalive proves insufficient for very long
`docker exec` streams. SSH (`DOCKER_HOST=ssh://linux`) remains the
default — TCP is opt-in via `make grade-test DOCKER_HOST=tcp://10.0.0.3:2375`.

**Security stance**: bind to the LAN-facing IPv4 only (`10.0.0.3`), never
`0.0.0.0`. Public IPv6 + Tailscale + WireGuard interfaces on this box
would otherwise be reachable. No TLS — relies on LAN trust + an explicit
ufw allow for `10.0.0.0/24` only.

## Apply (on `wy-linux`, run as `wy` with sudo)

```bash
# 1. Drop-in unit overlay: keep the existing fd:// socket-activation
#    listener, ADD a TCP listener bound to the LAN IP only.
sudo install -d -m 0755 /etc/systemd/system/docker.service.d

sudo tee /etc/systemd/system/docker.service.d/tcp-bind.conf >/dev/null <<'EOF'
# Added for OpenBot eval grading harness (see openbot/docs/runbooks/
# expose-remote-docker-tcp.md). Binds an additional TCP listener to the
# LAN IPv4 only — public IPv6, Tailscale, WireGuard interfaces are NOT
# exposed. The original `-H fd://` is preserved so existing systemd
# socket activation keeps working.
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd -H fd:// -H tcp://10.0.0.3:2375 --containerd=/run/containerd/containerd.sock
EOF

# 2. Reload systemd + restart docker. Existing containers keep running.
sudo systemctl daemon-reload
sudo systemctl restart docker

# 3. Firewall: explicitly allow 10.0.0.0/24 → :2375, drop everyone else.
sudo ufw allow from 10.0.0.0/24 to any port 2375 proto tcp comment 'docker-api-lan'
sudo ufw deny  from any         to any port 2375 proto tcp comment 'docker-api-deny-other'

# 4. Verify
ss -tlnp | grep :2375    # should show 10.0.0.3:2375, NOT 0.0.0.0:2375 or :::2375
docker -H tcp://10.0.0.3:2375 version | head
```

## From the Mac dev box

```bash
# Smoke
DOCKER_HOST=tcp://10.0.0.3:2375 docker version

# Use for one grade-test run (overrides Makefile default)
make -C evals grade-test DOCKER_HOST=tcp://10.0.0.3:2375 ...
```

## Revert (if you ever want it gone)

```bash
sudo rm /etc/systemd/system/docker.service.d/tcp-bind.conf
sudo systemctl daemon-reload
sudo systemctl restart docker
sudo ufw delete allow from 10.0.0.0/24 to any port 2375
sudo ufw delete deny  from any to any port 2375
```
