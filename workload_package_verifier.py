"""Independent reviewed-package byte verification; not runtime capability proof.

Only uncompressed, bounded USTAR component archives are accepted. No archive is
extracted and no code, manifest or digest supplied by the candidate is trusted.
Docker collection must independently pin the stopped candidate container/image and
fixed component paths; this module intentionally has no Docker or filesystem API.

Evidence framework_version is the reviewed package dependency requirement, NOT
proof of the candidate's installed framework. The independent image verifier must
still measure/verify the installed framework and execute the scoped protocol.
Package byte evidence alone never grants artifact or operational capability.
"""
import hashlib
import json
from types import MappingProxyType

PACKAGE_VERSION = "1.0.0"
FRAMEWORK_VERSION = "12.69.3"
PACKAGE_DIGEST = "b0a6dcf746f0e2a22393bd9ed44e6e020e1e94965b46bf7c3b08f854006aafa5"
# Reviewed source snapshot. Changes require a fresh explicit trust-anchor review.
TRUSTED_MANIFEST = MappingProxyType({
    "composer.json": "79e19d1aff6f90e303234b95c24ef4a665191bc266ffa79817549a4666a01af0",
    "config/deploy-monitor.php": "fb527700fcd1a07f5a0844df66d3cace218e0d86f7c1a426e295ee778d4ce93d",
    "database/migrations/2026_10_11_000001_create_workload_instrumentation.php": "7cc426eea4bbf86d362da5d7cd36681b399e4d203a19286d9ae56aad0fcad873",
    "database/schema.sql": "751988ad7b1b52eff463a6ea832dbc6d9e49063e3dbba393d6177fa2b262e03c",
    "src/Configuration.php": "7fcf8cc8ed7507d152ac948747c5b8cfbbc7e82a1dd6ab3bf3e894e70550f689",
    "src/HealthCommand.php": "4c4d786a81d0ecbd81e3daec1f9babdfe94b4abeabe0ac2a21ab3d0df9968f88",
    "src/PackageIdentity.php": "579628c82555c88a385abb4667d60e4a86a247f58505d759355749a1607235ed",
    "src/Protocol.php": "7834db3cdeef3f921caf803975f372072d5014340730202be3fb36bdeb31d156",
    "src/ProtocolCommand.php": "921b95333735d767372ac3fbae5fcfa9ee7a0aed6c69b89b636ff05a840ac508",
    "src/ProtocolError.php": "8a89355dab900bf045ac362584d3d28b04cfa7da2ab1f5616a211d1318e4607e",
    "src/Runtime.php": "e5ab08b1d4e3ad5fa596efbde266250de880c54b167a94d1ca09148bbc77c6e6",
    "src/SchedulerCommand.php": "49578271a007f1d2e9241b36735b5ba7467206f867ec27f54bcaa037da598648",
    "src/WorkloadServiceProvider.php": "76beaf9b6f7743db64ffb36c7b468c64cb94b0c615e4608087a72d529b44f79a"
})
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024
MAX_ENTRIES = 64
COMPONENTS = ("composer.json", "src", "config", "database")


class PackageVerificationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__("Workload package archive rejected: " + code)


def _reject(code):
    raise PackageVerificationError(code)


def _text(value):
    parts = value.split(b"\0", 1)
    if len(parts) == 2 and any(parts[1]):
        _reject("invalid-header")
    try:
        return parts[0].decode("ascii")
    except UnicodeError:
        _reject("invalid-header")


def _number(value):
    value = value.strip(b" \0")
    if not value or any(c not in b"01234567" for c in value):
        _reject("invalid-header")
    return int(value, 8)


def _path(name, directory):
    if directory and name.endswith("/"):
        name = name[:-1]
    if not name or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
        _reject("unsafe-path")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        _reject("unsafe-path")
    return name


def _read(archive, component=None):
    if type(archive) is not bytes or len(archive) > MAX_ARCHIVE_BYTES:
        _reject("archive-limit")
    if len(archive) < 1024 or len(archive) % 512:
        _reject("invalid-archive")
    expected = {p for p in TRUSTED_MANIFEST if component is None or p == component or p.startswith(component + "/")}
    directories = {p.rsplit("/", 1)[0] for p in expected if "/" in p}
    directories |= {p.split("/")[0] for p in expected if "/" in p}
    seen, result = set(), {}
    offset = 0
    while offset + 512 <= len(archive):
        header = archive[offset:offset + 512]
        if not any(header):
            # POSIX end marker plus arbitrary bounded zero record padding.
            if offset + 1024 > len(archive) or any(archive[offset:]):
                _reject("invalid-trailer")
            return result
        if len(seen) >= MAX_ENTRIES:
            _reject("entry-limit")
        checksum = _number(header[148:156])
        if checksum != sum(header[:148]) + 8 * 32 + sum(header[156:]):
            _reject("invalid-checksum")
        if header[257:265] != b"ustar\x0000" or any(header[500:512]):
            _reject("unsupported-format")
        kind = header[156:157]
        if kind not in (b"0", b"\0", b"5") or any(header[157:257]):
            _reject("unsupported-entry")
        name = _text(header[:100])
        prefix = _text(header[345:500])
        if prefix:
            name = prefix + "/" + name
        name = _path(name, kind == b"5")
        if name in seen:
            _reject("duplicate-entry")
        seen.add(name)
        size = _number(header[124:136])
        if size > MAX_FILE_BYTES:
            _reject("file-limit")
        offset += 512
        end = offset + size
        padded = offset + ((size + 511) // 512) * 512
        if padded > len(archive):
            _reject("truncated-entry")
        if any(archive[end:padded]):
            _reject("invalid-padding")
        if kind == b"5":
            if size != 0 or name not in directories:
                _reject("unexpected-entry")
        else:
            if name not in expected:
                _reject("unexpected-entry")
            result[name] = hashlib.sha256(archive[offset:end]).hexdigest()
        offset = padded
    _reject("missing-trailer")


def _evidence(actual):
    if actual != dict(TRUSTED_MANIFEST):
        _reject("manifest-mismatch")
    digest = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
    if digest != PACKAGE_DIGEST:
        _reject("invalid-trust-anchor")
    return {"package_version": PACKAGE_VERSION, "framework_version": FRAMEWORK_VERSION,
            "package_digest": digest, "manifest_digest": digest, "file_count": len(actual)}


def verify_archive(archive):
    """Verify a canonical combined archive whose roots are the four components."""
    return _evidence(_read(archive))


def verify_archives(archives):
    """Verify fixed Docker-cp component archives, including directory root headers.

    Caller must bound each stream while collecting it, before constructing bytes.
    No filename mapping, expected digest or manifest is accepted from the caller.
    """
    if type(archives) is not dict or set(archives) != set(COMPONENTS):
        _reject("invalid-components")
    actual = {}
    for component in COMPONENTS:
        actual.update(_read(archives[component], component))
    return _evidence(actual)
