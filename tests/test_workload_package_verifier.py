import hashlib
import io
import json
from pathlib import Path
import tarfile
import unittest

import workload_package_verifier as verifier

ROOT = Path(__file__).resolve().parents[1] / 'workload-instrumentation'


def archive(entries, *, format=tarfile.USTAR_FORMAT):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode='w', format=format) as stream:
        for name, data, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = 'composer.json'
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
    return target.getvalue()


def entries():
    return [(name, (ROOT / name).read_bytes(), tarfile.REGTYPE)
            for name in verifier.TRUSTED_MANIFEST]


def rewrite_header(data, field, value):
    value_bytes = bytearray(data)
    value_bytes[field] = value
    value_bytes[148:156] = b' ' * 8
    checksum = sum(value_bytes[:512])
    value_bytes[148:156] = ('%06o\0 ' % checksum).encode()
    return bytes(value_bytes)


class PackageVerifierTests(unittest.TestCase):
    def rejected(self, data, code=None):
        with self.assertRaises(verifier.PackageVerificationError) as caught:
            verifier.verify_archive(data)
        if code:
            self.assertEqual(code, caught.exception.code)
        self.assertNotIn('PRIVATE-CONTENT', str(caught.exception))

    def test_explicit_reviewed_source_manifest_matches_without_package_execution(self):
        # Enumerate source independently so an added source file fails this test.
        paths = [ROOT / 'composer.json']
        for directory in ('src', 'config', 'database'):
            for path in (ROOT / directory).rglob('*'):
                self.assertFalse(path.is_symlink())
                if path.is_file():
                    paths.append(path)
        actual = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        self.assertEqual(dict(verifier.TRUSTED_MANIFEST), actual)
        digest = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(verifier.PACKAGE_DIGEST, digest)
        result = verifier.verify_archive(archive(entries()))
        self.assertEqual(13, result['file_count'])
        self.assertEqual('12.69.3', result['framework_version'])
        self.assertNotIn('operational', result)

    def test_component_roots_and_docker_style_directory_headers(self):
        components = {}
        for component in verifier.COMPONENTS:
            items = [e for e in entries() if e[0] == component or e[0].startswith(component + '/')]
            if component != 'composer.json':
                items.insert(0, (component + '/', b'', tarfile.DIRTYPE))
            if component == 'database':
                items.insert(1, ('database/migrations/', b'', tarfile.DIRTYPE))
            components[component] = archive(items)
        self.assertEqual(verifier.PACKAGE_DIGEST, verifier.verify_archives(components)['package_digest'])
        components['src'] = archive(entries())
        with self.assertRaises(verifier.PackageVerificationError):
            verifier.verify_archives(components)

    def test_missing_altered_extra_and_duplicate_files_rejected(self):
        original = entries()
        for items in (original[:-1], [('composer.json', b'PRIVATE-CONTENT', tarfile.REGTYPE)] + original[1:],
                      original + [('src/extra.php', b'', tarfile.REGTYPE)], original + original[:1]):
            with self.subTest(entries=len(items)):
                self.rejected(archive(items))

    def test_unsafe_paths_rejected_without_extraction(self):
        for name in ('/composer.json', '../composer.json', 'src/../composer.json', './composer.json',
                     'src//Protocol.php', 'src\\Protocol.php', 'src/\nsecret'):
            with self.subTest(name=name):
                self.rejected(archive([(name, b'', tarfile.REGTYPE)]), 'unsafe-path')

    def test_links_devices_fifo_sparse_and_extensions_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE,
                     tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE, tarfile.XHDTYPE,
                     tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            with self.subTest(kind=kind):
                self.rejected(archive([('composer.json', b'', kind)]), 'unsupported-entry')

    def test_corrupt_checksum_truncation_trailer_and_padding(self):
        data = archive(entries())
        corrupt = bytearray(data)
        corrupt[0] ^= 1
        self.rejected(bytes(corrupt), 'invalid-checksum')
        self.rejected(data[:-1], 'invalid-archive')
        self.rejected(data[:512], 'invalid-archive')
        self.rejected(data[:1024], 'missing-trailer')
        self.rejected(data + b'x' * 512, 'invalid-trailer')
        corrupt = bytearray(data)
        corrupt[512 + len(entries()[0][1])] = 1
        self.rejected(bytes(corrupt), 'invalid-padding')
        truncated = rewrite_header(data[:1024], slice(124, 136), b'00000100000\0')
        self.rejected(truncated, 'truncated-entry')

    def test_limits_invalid_types_and_encoded_sizes(self):
        data = archive(entries())
        self.rejected(b'\0' * (verifier.MAX_ARCHIVE_BYTES + 512), 'archive-limit')
        self.rejected(bytearray(data), 'archive-limit')
        for number in (b'77777777777\0', b'\x80' + b'\0' * 11, b'0000000000x\0'):
            with self.subTest(number=number):
                self.rejected(rewrite_header(data, slice(124, 136), number))
        self.rejected(archive(entries(), format=tarfile.GNU_FORMAT), 'unsupported-format')
        self.rejected(b'\x1f\x8b' + b'\0' * 1022)
        with self.assertRaises(verifier.PackageVerificationError):
            verifier.verify_archives({})

    def test_two_zero_block_end_and_bounded_record_padding_accepted(self):
        data = archive(entries())
        last = 0
        for _, content, _ in entries():
            last += 512 + ((len(content) + 511) // 512) * 512
        self.assertEqual(verifier.PACKAGE_DIGEST, verifier.verify_archive(data[:last] + b'\0' * 1024)['package_digest'])
        self.rejected(data[:last] + b'\0' * 512, 'invalid-trailer')
        with self.assertRaises(TypeError):
            verifier.TRUSTED_MANIFEST['composer.json'] = '0' * 64


if __name__ == '__main__':
    unittest.main()
