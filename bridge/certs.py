"""TLS 自签证书：生成、加载、指纹与配对 URI。

证书指纹是凭据固定（pinning）的唯一依据：
``SHA-256(证书 DER)`` 的小写十六进制（PROTOCOL §2）。
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

#: 自签证书有效期（天）。
DEFAULT_VALIDITY_DAYS = 3650
#: RSA 密钥长度。
RSA_KEY_SIZE = 2048
#: 自签证书 Subject CN。
COMMON_NAME = "DSH Phone Bridge"


def cert_fingerprint(cert: x509.Certificate | bytes | str | os.PathLike[str]) -> str:
    """返回证书指纹：``SHA-256(DER)`` 小写 hex（64 字符）。

    参数可以是 ``x509.Certificate``、DER/PEM 字节，或证书文件路径。
    """
    if isinstance(cert, x509.Certificate):
        der = cert.public_bytes(serialization.Encoding.DER)
    elif isinstance(cert, (bytes, bytearray)):
        der = bytes(cert)
        if b"-----BEGIN CERTIFICATE-----" in der:
            cert = x509.load_pem_x509_certificate(der)
            der = cert.public_bytes(serialization.Encoding.DER)
    else:
        path = Path(cert)
        der = path.read_bytes()
        if b"-----BEGIN CERTIFICATE-----" in der:
            der = x509.load_pem_x509_certificate(der).public_bytes(
                serialization.Encoding.DER
            )
    return hashlib.sha256(der).hexdigest()


def load_certificate(path: str | os.PathLike[str]) -> x509.Certificate:
    """加载 PEM 证书。"""
    p = Path(path)
    data = p.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" in data:
        return x509.load_pem_x509_certificate(data)
    return x509.load_der_x509_certificate(data)


def _san_entries(host: str) -> list[x509.GeneralName]:
    entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    host = (host or "").strip().strip("[]")
    if not host or host in {"localhost"}:
        return entries
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        entries.append(x509.DNSName(host))
    else:
        if ip not in {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}:
            entries.append(x509.IPAddress(ip))
    return entries


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def create_self_signed_cert(
    host: str,
    cert_path: str | os.PathLike[str],
    key_path: str | os.PathLike[str],
    *,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
    common_name: str = COMMON_NAME,
) -> x509.Certificate:
    """生成自签 RSA-2048 证书并写盘（PEM），返回证书对象。"""
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    )
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName(_san_entries(host)), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
    )
    cert = builder.sign(private_key=key, algorithm=hashes.SHA256())

    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    _write_atomic(key_path, key_pem)
    _write_atomic(cert_path, cert_pem)
    try:  # 尽力收紧权限（Windows 上作用有限，失败忽略）
        os.chmod(key_path, 0o600)
    except OSError:  # pragma: no cover
        pass
    return cert


def load_or_create_cert(
    host: str,
    cert_path: str | os.PathLike[str],
    key_path: str | os.PathLike[str],
    *,
    force: bool = False,
    validity_days: int = DEFAULT_VALIDITY_DAYS,
    common_name: str = COMMON_NAME,
) -> x509.Certificate:
    """存在则加载证书，缺失（或 ``force``）则重新生成。

    证书与私钥必须同时存在，否则重新生成一对（避免指纹与私钥不匹配）。
    """
    cert_path = Path(cert_path)
    key_path = Path(key_path)
    if not force and cert_path.is_file() and key_path.is_file():
        return load_certificate(cert_path)
    return create_self_signed_cert(
        host,
        cert_path,
        key_path,
        validity_days=validity_days,
        common_name=common_name,
    )


def derive_server_id(fingerprint: str) -> str:
    """由证书指纹派生稳定的 ``serverId``：``srv-`` + 指纹前 12 位。"""
    fp = fingerprint.strip().lower()
    if len(fp) < 12:
        raise ValueError(f"非法证书指纹: {fingerprint!r}")
    return f"srv-{fp[:12]}"


def pair_uri(
    *,
    host: str,
    port: int,
    fingerprint: str,
    code: str,
    server_id: str,
    version: int = 1,
) -> str:
    """构造配对二维码 URI（PROTOCOL §5.1）。

    例：``dsphone://pair?v=1&host=192.168.1.10&port=8765&fp=<64hex>&code=123456&sid=srv-1a2b``

    ``host`` 必须是具体监听地址（调用方应已拒绝 0.0.0.0/::）。
    """
    from urllib.parse import quote

    host = (host or "").strip()
    if not host or host.strip("[]") in {"0.0.0.0", "::", "*"}:
        raise ValueError(f"配对 URI 不接受通配监听地址: {host!r}")
    host_param = quote(host.strip("[]"), safe="")
    return (
        f"dsphone://pair?v={version}"
        f"&host={host_param}"
        f"&port={int(port)}"
        f"&fp={fingerprint.strip().lower()}"
        f"&code={code}"
        f"&sid={quote(server_id, safe='')}"
    )


def server_ssl_context(
    cert_path: str | os.PathLike[str],
    key_path: str | os.PathLike[str],
    *,
    minimum_version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_2,
) -> ssl.SSLContext:
    """构造服务端 TLS 上下文（TLS ≥ 1.2，禁用压缩）。"""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = minimum_version
    context.options |= getattr(ssl, "OP_NO_COMPRESSION", 0)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


def san_hosts(cert: x509.Certificate) -> Iterable[str]:  # pragma: no cover - 诊断用
    """返回证书 SAN 中的 DNS/IP 条目（诊断输出用）。"""
    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return ()
    out: list[str] = []
    for name in ext.value:
        if isinstance(name, x509.DNSName):
            out.append(name.value)
        elif isinstance(name, x509.IPAddress):
            out.append(str(name.value))
    return tuple(out)
