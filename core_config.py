"""Lógica pura: lê o .conf WireGuard da Proton e gera o config do núcleo (mihomo)."""
import json
import re

PROXY_NAME = "DiscordVPN"

# Só estes processos vão pela VPN. Todo o resto do PC continua direto.
# (Update.exe fica de fora de propósito: o nome é genérico e outros apps usam.)
DISCORD_EXES = ["Discord.exe", "DiscordPTB.exe", "DiscordCanary.exe", "DiscordDevelopment.exe"]

LAN_RANGES = [
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "224.0.0.0/4", "255.255.255.255/32",
]


class ConfigError(Exception):
    pass


def parse_wireguard(text: str) -> dict:
    sections: dict[str, dict[str, str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            current = sections.setdefault(m.group(1).lower(), {})
            continue
        if "=" in line and current is not None:
            k, v = line.split("=", 1)  # chaves base64 terminam com '=', por isso split(…, 1)
            current[k.strip().lower()] = v.strip()

    iface, peer = sections.get("interface"), sections.get("peer")
    if not iface or not peer:
        raise ConfigError("Arquivo inválido: faltam as seções [Interface] e [Peer].")
    try:
        private_key = iface["privatekey"]
        public_key = peer["publickey"]
        endpoint = peer["endpoint"]
    except KeyError as e:
        raise ConfigError(f"Campo ausente no .conf: {e.args[0]}")

    host, _, port = endpoint.strip().rpartition(":")
    host = host.strip("[]")
    if not host or not port.isdigit():
        raise ConfigError("Endpoint inválido no .conf.")

    ipv4 = next((a.strip() for a in iface.get("address", "").split(",")
                 if a.strip() and ":" not in a), None)
    if not ipv4:
        raise ConfigError("O .conf não tem Address IPv4.")

    return {
        "private_key": private_key,
        "public_key": public_key,
        "server": host,
        "port": int(port),
        "ip": ipv4,
        "psk": peer.get("presharedkey"),
        "mtu": int(iface["mtu"]) if iface.get("mtu", "").isdigit() else None,
    }


def build_yaml(wg: dict, secret: str, api_port: int = 9097) -> str:
    q = json.dumps  # string JSON também é YAML válido
    L = [
        "log-level: warning",
        "mode: rule",
        "ipv6: true",
        "allow-lan: false",
        "unified-delay: true",
        "find-process-mode: always",
        f"external-controller: 127.0.0.1:{api_port}",
        f"secret: {q(secret)}",
        "",
        "tun:",
        "  enable: true",
        "  stack: mixed",
        "  device: DiscordVPN",
        "  auto-route: true",
        "  auto-detect-interface: true",
        "  strict-route: false",
        "  dns-hijack: []",
        "",
        "dns:",
        "  enable: true",
        "  ipv6: false",
        "  nameserver:",
        "    - 1.1.1.1",
        "    - 8.8.8.8",
        "",
        "proxies:",
        f"  - name: {q(PROXY_NAME)}",
        "    type: wireguard",
        f"    server: {q(wg['server'])}",
        f"    port: {wg['port']}",
        f"    ip: {q(wg['ip'])}",
        f"    private-key: {q(wg['private_key'])}",
        f"    public-key: {q(wg['public_key'])}",
        '    allowed-ips: ["0.0.0.0/0"]',
        "    udp: true",
    ]
    if wg.get("psk"):
        L.append(f"    pre-shared-key: {q(wg['psk'])}")
    if wg.get("mtu"):
        L.append(f"    mtu: {wg['mtu']}")

    L += ["", "rules:"]
    for cidr in LAN_RANGES:
        L.append(f"  - IP-CIDR,{cidr},DIRECT,no-resolve")
    # IPv6 do Discord é bloqueado: o túnel da Proton free é só IPv4 e o IPv6 vazaria seu IP real.
    # O Discord cai sozinho para IPv4.
    for exe in DISCORD_EXES:
        L.append(f"  - AND,((IP-CIDR6,::/0),(PROCESS-NAME,{exe})),REJECT")
    for exe in DISCORD_EXES:
        L.append(f"  - PROCESS-NAME,{exe},{PROXY_NAME}")
    L.append("  - MATCH,DIRECT")
    return "\n".join(L) + "\n"
