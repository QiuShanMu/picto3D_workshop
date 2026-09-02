from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import ssl
from datetime import datetime, timedelta, timezone

from pipeline.scan.app import create_app


def _lan_ips() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses."""
    ips: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return ips


def _make_self_signed_cert(cert_path: str, key_path: str, host: str, port: int) -> None:
    """Generate a self-signed cert (with LAN IP SANs) if it does not exist yet."""
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:  # pragma: no cover - cryptography is a hard requirement here
        raise SystemExit(
            "HTTPS 需要 cryptography。请安装：pip install cryptography"
        )

    country = "CN"
    org = "p3d-scan"
    common_name = host if host not in ("0.0.0.0", "::") else "localhost"

    # Build SAN entries: IPs (incl. 127.0.0.1) + common name (if it looks like an IP)
    san_ips = ["127.0.0.1"]
    for ip in _lan_ips():
        if ip not in san_ips:
            san_ips.append(ip)
    if host not in ("0.0.0.0", "::", ""):
        try:
            ipaddress.ip_address(host)
            if host not in san_ips:
                san_ips.append(host)
        except ValueError:
            pass

    name = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, country),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])

    san = x509.SubjectAlternativeName([
        x509.IPAddress(ipaddress.ip_address(ip)) for ip in san_ips
    ])

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )

    os.makedirs(os.path.dirname(cert_path) or ".", exist_ok=True)
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SKU scan service (phone as camera)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5070)
    parser.add_argument(
        "--https", action="store_true",
        help="Serve over HTTPS (auto-generate a self-signed cert). Use for phone camera access.",
    )
    parser.add_argument(
        "--cert", default=None,
        help="Path to a custom PEM certificate (only valid with --https).",
    )
    parser.add_argument(
        "--key", default=None,
        help="Path to the matching PEM private key (only valid with --https).",
    )
    args = parser.parse_args(argv)

    app = create_app()
    scheme = "http"
    ssl_context = None

    if args.https:
        scheme = "https"
        if args.cert and args.key:
            ssl_context = (args.cert, args.key)
        else:
            cert_dir = os.path.join(".p3d", "cert")
            cert_path = os.path.join(cert_dir, "scan.crt")
            key_path = os.path.join(cert_dir, "scan.key")
            _make_self_signed_cert(cert_path, key_path, args.host, args.port)
            ssl_context = (cert_path, key_path)
            print(f"  Self-signed cert : {cert_path}")

    access_host = args.host
    if access_host in ("0.0.0.0", "::"):
        ips = _lan_ips()
        access_host = ips[0] if ips else "localhost"

    print("SKU scan service:")
    print(f"  Phone camera page : {scheme}://{access_host}:{args.port}/scan")
    print(f"  Desktop result page: {scheme}://{access_host}:{args.port}/")
    print("  NOTE: phone browsers require HTTPS (or localhost) to access the camera.")

    # Use make_server (not app.run) because the Werkzeug dev server's TLS path
    # is more reliable across Windows/backgrounded shells.
    from werkzeug.serving import make_server

    server = make_server(
        args.host,
        args.port,
        app,
        threaded=True,
        ssl_context=ssl_context,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
