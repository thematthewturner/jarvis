# My OpenClaw Server — DigitalOcean Droplet

## Droplet Details
| Field | Value |
|-------|-------|
| **Name** | openclaw-ubuntu-s-1vcpu-2gb-70gb-intel-sfo2-01 |
| **Project** | first-project |
| **OS** | OpenClaw 2.12 on Ubuntu |
| **Region** | SFO2 (San Francisco 2) |
| **IPv4** | 165.22.138.56 |
| **Private IP** | 10.120.0.2 |
| **IPv6** | Not enabled |
| **Reserved IP** | Not enabled |

## Hardware
| Resource | Amount |
|----------|--------|
| **Memory** | 2 GB |
| **CPU** | 1 Intel vCPU |
| **Disk** | 70 GB |

## Status
- **State:** ON
- **CPU usage:** ~50–55% sustained (as observed)
- **Load:** ~1.0–1.5 (1/5/15 avg)

## Gateway
- Default port: `18789`
- WebSocket: `ws://127.0.0.1:18789`
- Control UI: `http://127.0.0.1:18789/`

## AI Config
- Provider: Anthropic (Claude API)
- Recommended model: `anthropic/claude-opus-4-6`

## SSH Access
```bash
ssh root@165.22.138.56
su - openclaw   # always switch to openclaw user first
```

## Notes
- CPU is running high (~50%) — may want to monitor or upsize if adding more channels/skills
- Private IP 10.120.0.2 available for VPC networking within SFO2
- Droplet name follows DO naming convention: `<app>-<os>-<size>-<region>-<index>`
