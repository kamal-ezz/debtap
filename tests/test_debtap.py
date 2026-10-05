"""Regression checks for conversion safety; no root access or network required.

Run with: python3 -m unittest discover -s tests -v
"""
import gzip
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'debtap'
SOURCE = SCRIPT.read_text()
HELPERS = SOURCE[SOURCE.index('# Move all entries'):SOURCE.index('# Options, help, database')]
CACHE_NAMES = [f'debian-{part}-packages-files' for part in ('main', 'non-free', 'contrib')] + [
    'ubuntu-packages-files', 'virtual-packages', 'aur-packages', 'extended-base-packages-list']


def archive(path, files):
    with tarfile.open(path, 'w:gz') as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))


class DebtapRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='conversion-tests-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work_temp = self.root / 'temporary work [literal]'
        self.work_temp.mkdir()
        self.env = dict(os.environ, TERM='dumb', TMPDIR=str(self.work_temp))
        self.addCleanup(lambda: self.assertEqual(list(self.work_temp.iterdir()), []))

    def bash(self, code, args=(), input=None):
        return subprocess.run(['bash', '-c', code, 'test', *map(str, args)], cwd=self.root,
                              env=self.env, text=True, capture_output=True, timeout=30,
                              input=input)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def make_deb(self, corrupt=None, control=None, payload=None):
        self.write('debian-binary', '2.0\n')
        if control is None:
            control = {'control': 'Package: demoapp\nVersion: 1.0\nArchitecture: all\n'
                       'Description: Sample package\n'}
        if payload is None:
            payload = {'lib/.hidden': 'hidden', 'lib/shared/a': 'first',
                       'usr/lib/shared/b': 'second'}
        archive(self.root / 'control.tar.gz', control)
        archive(self.root / 'data.tar.gz', payload)
        if corrupt:
            self.write(corrupt, 'broken archive')
        result = subprocess.run(['ar', 'rc', 'demo.deb', 'debian-binary', 'control.tar.gz',
                                 'data.tar.gz'], cwd=self.root, capture_output=True, text=True)
        self.assert_ok(result)
        return self.root / 'demo.deb'

    def test_output_directory_with_spaces_and_globs(self):
        output = self.root / 'output [literal] with spaces'
        output.mkdir()
        code = SOURCE[SOURCE.index('# Options, help, database'):SOURCE.index('if [[ $pseudo == set ]]')]
        result = self.bash(code + '\nprintf "%s\\n" "$outputdirectory"', ['-o', output, 'demo.deb'])
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), str(output))

    def test_report_contents_are_printed_without_execution(self):
        self.write('report', 'touch marker\n$(touch another)\n*\n')
        result = self.bash(HELPERS + '\nprint_package_report report')
        self.assert_ok(result)
        self.assertEqual(result.stdout, 'touch, marker, $(touch, another), *\n')
        self.assertFalse((self.root / 'marker').exists())
        self.assertFalse((self.root / 'another').exists())

    def test_migration_preserves_hidden_files_and_merges_directories(self):
        self.write('lib/.hidden', 'hidden')
        self.write('lib/shared/a', 'a')
        self.write('usr/lib/shared/b', 'b')
        self.assert_ok(self.bash(HELPERS + '\nmigrate_directory lib usr/lib'))
        self.assertEqual((self.root / 'usr/lib/.hidden').read_text(), 'hidden')
        self.assertEqual((self.root / 'usr/lib/shared/a').read_text(), 'a')
        self.assertEqual((self.root / 'usr/lib/shared/b').read_text(), 'b')
        self.assertFalse((self.root / 'lib').exists())

    def test_migration_collision_preserves_both_files(self):
        self.write('bin/demo', 'source')
        self.write('usr/bin/demo', 'destination')
        self.assertNotEqual(self.bash(HELPERS + '\nmigrate_directory bin usr/bin').returncode, 0)
        self.assertEqual((self.root / 'bin/demo').read_text(), 'source')
        self.assertEqual((self.root / 'usr/bin/demo').read_text(), 'destination')

    def test_failed_move_preserves_source(self):
        self.write('bin/demo', 'source')
        result = self.bash(HELPERS + '\nmv() { return 1; }; migrate_directory bin usr/bin')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'bin/demo').read_text(), 'source')

    def test_empty_directory_migration(self):
        (self.root / 'bin').mkdir()
        self.assert_ok(self.bash(HELPERS + '\nmigrate_directory bin usr/bin'))
        self.assertTrue((self.root / 'usr/bin').is_dir())
        self.assertFalse((self.root / 'bin').exists())

    def test_extraction_checks_ar_failure_even_after_valid_output(self):
        self.make_deb()
        code = HELPERS + '''
package_with_full_path=demo.deb
ar() { command ar "$@"; return 1; }
extract_deb_member data.tar.gz 'tar -xz'
'''
        self.assertNotEqual(self.bash(code).returncode, 0)
        self.assertTrue((self.root / 'lib/.hidden').exists())

    def conversion_script(self):
        # Redirect hardcoded caches; conversion workspaces use our TMPDIR.
        source = SOURCE.replace('/var/cache/debtap', str(self.root / 'cache'))
        source = source.replace('/var/cache/pkgfile', str(self.root / 'pkgfile-cache'))
        for name in CACHE_NAMES:
            self.write('cache/' + name, 'fixture\n')
        self.write('pkgfile-cache/core.files.123', 'fixture\n')
        mock = self.write('commands/namcap', '#!/bin/bash\nexit 0\n')
        mock.chmod(0o755)
        self.env['PATH'] = str(mock.parent) + os.pathsep + self.env['PATH']
        return self.write('isolated-debtap', source)

    def mock_command(self, name, code):
        mock = self.write('commands/' + name, '#!/bin/bash\n' + code)
        mock.chmod(0o755)

    def package_contents(self, directory=None):
        packages = list((directory or self.root).glob('*.pkg.tar.zst'))
        self.assertEqual(len(packages), 1)
        result = subprocess.run(['zstd', '-dc', str(packages[0])], capture_output=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return tarfile.open(fileobj=io.BytesIO(result.stdout))

    def test_epoch_dependencies_preserve_comparison_operators(self):
        dependencies = [
            ('libusb-1.0-0', 'libusb', '>= 2:1.0.16', '>=1.0.16'),
            ('libx11-6', 'libx11', '>= 2:1.4.99.1', '>=1.4.99.1'),
            ('libxcomposite1', 'libxcomposite', '>= 1:0.4.4-1', '>=0.4.4'),
            ('libxdamage1', 'libxdamage', '>= 1:1.1', '>=1.1'),
            ('libnspr4', 'nspr', '>= 2:4.9-2~', '>=4.9'),
            ('libnss3', 'nss', '>= 2:3.30', '>=3.30'),
        ]
        for database in ('debian-main-packages-files', 'ubuntu-packages-files'):
            with self.subTest(database=database):
                script = self.conversion_script()
                contents = ''.join(f'usr/bin/{arch_name} section/{deb_name}\n'
                                   for deb_name, arch_name, _, _ in dependencies)
                self.write('cache/' + database, contents)
                self.mock_command('pkgfile', 'printf "extra/%s\\n" "$1"\n')
                control = ('Package: demoapp\nVersion: 1.0\nArchitecture: all\n'
                           'Description: Sample package\nDepends: ' + ', '.join(
                               f'{name} ({constraint})' for name, _, constraint, _
                               in dependencies) + '\n')
                deb = self.make_deb(control={'control': control})
                output = self.root / database
                output.mkdir()
                self.assert_ok(self.bash('bash "$1" -Q -p -o "$2" "$3"',
                                         [script, output, deb]))
                with self.package_contents(output) as tar:
                    pkginfo = tar.extractfile('.PKGINFO').read().decode()
                actual = {line.removeprefix('depend = ') for line in pkginfo.splitlines()
                          if line.startswith('depend = ')}
                self.assertEqual(actual, {name + constraint for _, name, _, constraint
                                          in dependencies})
                pkgbuild = output / 'demoapp-PKGBUILD/PKGBUILD'
                result = self.bash('source "$1"; printf "%s\\n" "${depends[@]}"',
                                   [pkgbuild])
                self.assert_ok(result)
                self.assertEqual(set(result.stdout.splitlines()), actual)

    def test_existing_working_directory_is_preserved(self):
        script = self.conversion_script()
        deb = self.make_deb()
        marker = self.write('demo-working-directory/user-work', 'preserve me')
        self.assert_ok(self.bash('bash "$1" -Q "$2"', [script, deb]))
        self.assertEqual(marker.read_text(), 'preserve me')

    def test_payload_names_cannot_collide_with_scratch_or_control_files(self):
        script = self.conversion_script()
        payload = {name: 'payload: ' + name for name in (
            'tempfile-important', 'control', 'preinst', 'postinst', 'conffiles',
            'pkgbuildinstallations1', 'namcap-checks', 'test.pkg.tar', 'data.tar',
            '.hidden', 'file with spaces', 'line\nbreak', '--checkpoint=1',
            'usr/share/demo')}
        deb = self.make_deb(payload=payload)
        self.assert_ok(self.bash('bash "$1" -Q -p "$2"', [script, deb]))
        with self.package_contents() as tar:
            files = {item.name: tar.extractfile(item).read()
                     for item in tar if item.isfile()}
            for name, content in payload.items():
                self.assertEqual(files[name], content.encode())
            self.assertEqual(set(files), set(payload) | {'.PKGINFO', '.MTREE'})
            self.assertTrue(all(item.uid == 0 and item.gid == 0 for item in tar))
            mtree = gzip.decompress(files['.MTREE'])
            self.assertIn(b'tempfile-important', mtree)
            self.assertIn(b'.hidden', mtree)
        pkgbuild = self.root / 'demoapp-PKGBUILD/PKGBUILD'
        pkgdir = self.root / 'pkgdir'
        pkgdir.mkdir()
        self.assert_ok(self.bash('source "$1"; pkgdir=$2; package', [pkgbuild, pkgdir]))
        for name, content in payload.items():
            self.assertEqual((pkgdir / name).read_text(), content)

    def test_invalid_identity_is_rejected_without_execution(self):
        script = self.conversion_script()
        marker = self.root / 'executed'
        for field in ('Package', 'Version', 'Architecture'):
            with self.subTest(field=field):
                fields = {'Package': 'demoapp', 'Version': '1.0', 'Architecture': 'all'}
                fields[field] = f'demo$(echo>{marker})'
                deb = self.make_deb(control={'control': ''.join(
                    f'{key}: {value}\n' for key, value in fields.items())})
                result = self.bash('bash "$1" -Q "$2"', [script, deb])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Invalid package', result.stderr)
                self.assertFalse(marker.exists())
                self.assertEqual(list(self.root.glob('*.pkg.tar.zst')), [])

    def test_edited_identity_is_revalidated(self):
        script = self.conversion_script()
        deb = self.make_deb()
        marker = self.root / 'executed'
        self.env['EDITOR'] = 'edit-metadata'
        self.mock_command('edit-metadata',
                          f"printf '%s\\n' 'pkgname = demo$(echo>{marker})' "
                          "'pkgver = 1.0-1' 'arch = any' > \"$1\"\n")
        result = self.bash('bash "$1" -q "$2"', [script, deb], input='3')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Invalid package', result.stderr)
        self.assertFalse(marker.exists())

    def test_packaging_failures_never_publish_success(self):
        script = self.conversion_script()
        deb = self.make_deb()
        real_tar = shutil.which('tar')
        for stage, command, code, error in (
            ('mtree', 'bsdtar', 'exit 1\n', 'Cannot generate .MTREE'),
            ('archive', 'tar', f'"{real_tar}" "$@" || exit $?\n'
             'for arg; do [[ $arg != */package.tar ]] || exit 1; done\nexit 0\n',
             'Cannot create package archive'),
            ('compression', 'zstd',
             'while (( $# )); do if [[ $1 == -o ]]; then shift; '
             'printf partial > "$1"; break; fi; shift; done\nexit 1\n',
             'Cannot compress package archive'),
            ('publication', 'ln', 'exit 1\n', 'Cannot publish package'),
        ):
            with self.subTest(stage=stage):
                self.mock_command(command, code)
                try:
                    result = self.bash('bash "$1" -Q "$2"', [script, deb])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(error, result.stderr)
                    self.assertNotIn('Package successfully created', result.stdout)
                    self.assertEqual(list(self.root.glob('*.pkg.tar.zst')), [])
                    self.assertEqual(list(self.root.glob('.debtap-output.*')), [])
                    self.assertEqual(list(self.work_temp.iterdir()), [])
                finally:
                    (self.root / 'commands' / command).unlink()

    def test_existing_package_and_symlink_are_preserved(self):
        script = self.conversion_script()
        deb = self.make_deb()
        destination = self.write('demoapp-1.0-1-any.pkg.tar.zst', 'original package')
        result = self.bash('bash "$1" -Q "$2"', [script, deb])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot publish package', result.stderr)
        self.assertEqual(destination.read_text(), 'original package')
        destination.unlink()
        target = self.write('original', 'preserve me')
        destination.symlink_to(target)
        result = self.bash('bash "$1" -Q "$2"', [script, deb])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Cannot publish package', result.stderr)
        self.assertTrue(destination.is_symlink())
        self.assertEqual(target.read_text(), 'preserve me')
        self.assertEqual(list(self.root.glob('.debtap-output.*')), [])

    def test_empty_payload_can_be_packaged(self):
        script = self.conversion_script()
        deb = self.make_deb(payload={})
        self.assert_ok(self.bash('bash "$1" -Q "$2"', [script, deb]))
        with self.package_contents() as tar:
            self.assertEqual(set(tar.getnames()), {'.PKGINFO', '.MTREE'})

    def test_maintainer_script_is_packaged_separately_from_payload(self):
        script = self.conversion_script()
        deb = self.make_deb(control={
            'control': 'Package: demoapp\nVersion: 1.0\nArchitecture: all\n'
                       'Description: Sample package\n',
            'postinst': '#!/bin/sh\necho installed\n',
        }, payload={'postinst': 'payload postinst', 'usr/share/demo': 'demo'})
        self.assert_ok(self.bash('bash "$1" -Q -p "$2"', [script, deb]))
        with self.package_contents() as tar:
            self.assertEqual(tar.extractfile('postinst').read(), b'payload postinst')
            install = tar.extractfile('.INSTALL').read()
            self.assertIn(b'echo installed', install)
            self.assertNotIn(b'payload postinst', install)
            mtree = gzip.decompress(tar.extractfile('.MTREE').read())
            self.assertIn(b'.INSTALL', mtree)
        self.assertEqual((self.root / 'demoapp-PKGBUILD/demoapp.install').read_bytes(),
                         install)

    def test_reserved_payload_metadata_is_rejected(self):
        script = self.conversion_script()
        for name in ('.PKGINFO', '.INSTALL', '.MTREE'):
            with self.subTest(name=name):
                deb = self.make_deb(payload={name: 'payload'})
                result = self.bash('bash "$1" -Q "$2"', [script, deb])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('reserved package metadata', result.stderr)

    def test_termination_cleans_workspace(self):
        script = self.conversion_script()
        deb = self.make_deb()
        self.mock_command('namcap', 'kill -TERM "$PPID"\n')
        result = self.bash('exec bash "$1" -Q "$2"', [script, deb])
        self.assertEqual(result.returncode, 143, result.stdout + result.stderr)
        self.assertEqual(list(self.root.glob('*.pkg.tar.zst')), [])

    def test_concurrent_conversions_have_independent_workspaces(self):
        script = self.conversion_script()
        deb = self.make_deb()
        self.env['TEST_GATE'] = str(self.root / 'gate')
        self.mock_command('namcap', 'printf "%s\\n" "$PWD" > "$TEST_GATE.$PPID"\n'
                          'for ((i=0; i<500; i++)); do\n'
                          '  [[ ! -f $TEST_GATE ]] || exit 0\n'
                          '  sleep 0.01\ndone\nexit 1\n')
        processes = []
        try:
            for name in ('one', 'two'):
                output = self.root / name
                output.mkdir()
                processes.append(subprocess.Popen(
                    ['bash', str(script), '-Q', '-o', str(output), str(deb)],
                    cwd=self.root, env=self.env, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + 10
            while len(list(self.root.glob('gate.*'))) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            gates = list(self.root.glob('gate.*'))
            self.assertEqual(len(gates), 2)
            self.assertEqual(len({path.read_text() for path in gates}), 2)
            self.write('gate', 'continue')
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            for name in ('one', 'two'):
                with self.package_contents(self.root / name) as tar:
                    self.assertEqual(tar.extractfile('usr/lib/.hidden').read(), b'hidden')
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=15)

    def test_corrupt_archives_stop_conversion(self):
        script = self.conversion_script()
        for member in ('control.tar.gz', 'data.tar.gz'):
            with self.subTest(member=member):
                deb = self.make_deb(corrupt=member)
                result = self.bash('bash "$1" -Q -P "$2"', [script, deb])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Cannot extract package', result.stderr)
                self.assertFalse((self.root / 'demoapp-PKGBUILD').exists())
                self.assertFalse((self.root / 'demo-working-directory').exists())

    def test_generated_pkgbuild_migrates_and_preserves_existing_output(self):
        script = self.conversion_script()
        deb = self.make_deb()
        result = self.bash('bash "$1" -Q -P "$2"', [script, deb])
        self.assert_ok(result)
        pkgbuild = self.root / 'demoapp-PKGBUILD/PKGBUILD'
        self.assertTrue(pkgbuild.exists(), result.stdout + result.stderr)
        self.assert_ok(self.bash('bash -n "$1"', [pkgbuild]))
        pkgdir = self.root / 'pkgdir'
        pkgdir.mkdir()
        self.assert_ok(self.bash('source "$1"; pkgdir=$2; package', [pkgbuild, pkgdir]))
        self.assertEqual((pkgdir / 'usr/lib/.hidden').read_text(), 'hidden')
        self.assertEqual((pkgdir / 'usr/lib/shared/a').read_text(), 'first')
        self.assertEqual((pkgdir / 'usr/lib/shared/b').read_text(), 'second')
        pkgbuild.write_text('# user edits\n')
        result = self.bash('bash "$1" -Q -P "$2"', [script, deb])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(pkgbuild.read_text(), '# user edits\n')

    def test_conversion_creates_package_with_complete_migrated_payload(self):
        script = self.conversion_script()
        deb = self.make_deb()
        output = self.root / 'package output with spaces'
        output.mkdir()
        self.assert_ok(self.bash('bash "$1" -Q -o "$2" "$3"', [script, output, deb]))
        packages = list(output.glob('*.pkg.tar.zst'))
        self.assertEqual(len(packages), 1)
        result = subprocess.run(['zstd', '-dc', str(packages[0])], capture_output=True,
                                timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
            for path, content in [('usr/lib/.hidden', b'hidden'),
                                  ('usr/lib/shared/a', b'first'),
                                  ('usr/lib/shared/b', b'second')]:
                self.assertEqual(tar.extractfile(path).read(), content)
            self.assertIn('.PKGINFO', tar.getnames())
            self.assertIn('.MTREE', tar.getnames())

    def test_existing_output_directory_and_symlink_are_preserved(self):
        script = self.conversion_script()
        deb = self.make_deb()
        output = self.root / 'custom output'
        output.mkdir()
        target = self.root / 'edited'
        target.mkdir()
        (target / 'PKGBUILD').write_text('# edits\n')
        link = output / 'demoapp-PKGBUILD'
        link.symlink_to(target, target_is_directory=True)
        result = self.bash('bash "$1" -Q -P -o "$2" "$3"', [script, output, deb])
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(link.is_symlink())
        self.assertEqual((target / 'PKGBUILD').read_text(), '# edits\n')

    def test_pkgbuild_metadata_roundtrips_without_execution(self):
        hostile = '''Quotes " ' backslash \\ $HOME $(touch marker) `touch marker` ; *'''
        fields = {'pkgname': 'demo', 'pkgver': '1.0-1', 'pkgdesc': hostile, 'url': hostile,
                  'arch': 'any', 'license': hostile, 'depend': hostile, 'optdepend': hostile,
                  'provides': hostile, 'conflicts': hostile, 'replaces': hostile, 'backup': hostile}
        self.write('.PKGINFO', ''.join(f'{key} = {value}\n' for key, value in fields.items()))
        section = SOURCE[SOURCE.index('echo "# Generated by debtap"'):SOURCE.index('echo "options=')]
        code = HELPERS + '\nquiet=set\n' + section + '''
source PKGBUILD
printf '%s\\0' "$pkgdesc" "$url" "${license[0]}" "${depends[0]}" "${optdepends[0]}" "${provides[0]}" "${conflicts[0]}" "${replaces[0]}" "${backup[0]}"
'''
        result = self.bash(code)
        self.assert_ok(result)
        self.assertEqual(result.stdout.split('\0')[:-1], [hostile] * 9)
        self.assertFalse((self.root / 'marker').exists())

    def test_architecture_specific_arrays_remain_correct(self):
        self.write('.PKGINFO', 'pkgname = demo\npkgver = 1.0-1\narch = x86_64\n'
                   'depend = lib32-example\nbackup = usr/lib32/example.conf\n')
        section = SOURCE[SOURCE.index('echo "# Generated by debtap"'):SOURCE.index('echo "options=')]
        result = self.bash(HELPERS + '\nquiet=set\n' + section + '''
source PKGBUILD
printf '%s\\n' "${arch[*]}" "${depends_i686[0]}" "${depends_x86_64[0]}" "${backup_i686[0]}"
''')
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines(), ['i686 x86_64', 'example',
                                                     'lib32-example', 'usr/lib/example.conf'])

    def update_fixtures(self):
        for name in CACHE_NAMES:
            self.write('cache/' + name, 'old cache\n')
        self.write('index', ''.join(f'<option value="{name}" >{name}</option>\n'
                                   for name in ('stable', 'testing', 'future')))
        (self.root / 'contents.gz').write_bytes(gzip.compress(b'usr/bin/demo section/demo\n'))
        archive(self.root / 'virtual.tar.gz', {
            'virtual-packages-list-generator-master/virtual-packages': 'virtual-demo\n'})
        helpers = HELPERS.replace('/var/cache/debtap', str(self.root / 'cache'))
        mocks = '''
pkgfile() { return 0; }
uname() { printf 'x86_64\\n'; }
pacman() { printf 'Depends On : bash coreutils\\n'; }
pactree() { printf '%s\\n' "$2"; }
curl() {
    local url=$2 output=${4:-} fixture=contents.gz
    [[ $url == *packages.ubuntu.com ]] && fixture=index
    [[ $url == *github.com* ]] && fixture=virtual.tar.gz
    if [[ -n $fail && $url == *"$fail"* ]]; then
        # A complete gzip stream with a failing producer must still fail.
        [[ $fail == 'sid/main' ]] && cat contents.gz
        return 22
    fi
    if [[ -n $output ]]; then cp "$fixture" "$output"; else cat "$fixture"; fi
}
'''
        return helpers + mocks

    def dependency_setup_script(self, missing='', install_status=0, still_missing='',
                                euid=0):
        # Exercise the actual -u branch with mocked commands and root status.
        options = SOURCE[SOURCE.index('# Options, help, database'):
                         SOURCE.index('elif [[ "${@: -1}" == "debtap" ]]')]
        options = options.replace('$EUID != 0', '${test_euid:-0} != 0') + '\nfi\n'
        mocks = '''
command() {
    if [[ $1 == -v ]]; then
        case " $missing " in
            *" $2 "*) [[ $installed == set && $2 != "$still_missing" ]] ;;
            *) return 0 ;;
        esac
    else
        builtin command "$@"
    fi
}
pacman() {
    printf '%s\\n' "$@" > installation-args
    (( install_status == 0 )) || return "$install_status"
    installed=set
}
update_database() { printf updated > database-updated; }
'''
        setup = ('missing=$1; install_status=$2; still_missing=$3; test_euid=$4; '
                 'shift 4\n')
        return HELPERS + mocks + setup + options, [missing, install_status, still_missing,
                                                  euid, '-u']

    def test_update_installs_missing_dependencies_and_continues(self):
        code, args = self.dependency_setup_script('pkgfile pactree ar readelf zstd unzstd')
        result = self.bash(code, args, input='yes\n')
        self.assert_ok(result)
        self.assertIn('pkgfile: pkgfile', result.stdout)
        self.assertIn('pacman-contrib: pactree', result.stdout)
        self.assertIn('binutils: ar readelf', result.stdout)
        self.assertEqual((self.root / 'installation-args').read_text().splitlines(),
                         ['-S', '--needed', '--', 'pkgfile', 'pacman-contrib', 'binutils',
                          'zstd'])
        self.assertTrue((self.root / 'database-updated').exists())

    def test_update_with_dependencies_present_never_installs_or_prompts(self):
        code, args = self.dependency_setup_script()
        result = self.bash(code, args, input='')
        self.assert_ok(result)
        self.assertNotIn('Install these packages', result.stdout)
        self.assertFalse((self.root / 'installation-args').exists())
        self.assertTrue((self.root / 'database-updated').exists())

    def test_update_declining_or_eof_never_installs_or_updates(self):
        code, args = self.dependency_setup_script('pkgfile')
        for answer in ('n\n', '\n', ''):
            with self.subTest(answer=answer):
                result = self.bash(code, args, input=answer)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('sudo pacman -S --needed pkgfile', result.stderr)
                self.assertFalse((self.root / 'installation-args').exists())
                self.assertFalse((self.root / 'database-updated').exists())

    def test_update_quiet_flags_do_not_approve_installation(self):
        for flag in ('-uq', '-uQ'):
            with self.subTest(flag=flag):
                code, args = self.dependency_setup_script('pkgfile')
                args[-1] = flag
                result = self.bash(code, args, input='')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Install these packages', result.stdout)
                self.assertFalse((self.root / 'installation-args').exists())
                self.assertFalse((self.root / 'database-updated').exists())

    def test_update_install_failure_stops_before_database_changes(self):
        code, args = self.dependency_setup_script('pkgfile', install_status=1)
        result = self.bash(code, args, input='y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Dependency installation failed', result.stderr)
        self.assertFalse((self.root / 'database-updated').exists())

    def test_update_rechecks_commands_after_installation(self):
        code, args = self.dependency_setup_script('pkgfile', still_missing='pkgfile')
        result = self.bash(code, args, input='Y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('still unavailable after installation: pkgfile', result.stderr)
        self.assertFalse((self.root / 'database-updated').exists())

    def test_update_without_pacman_shows_setup_error(self):
        code, args = self.dependency_setup_script('pacman pkgfile')
        result = self.bash(code, args, input='y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Dependency setup requires pacman', result.stderr)
        self.assertFalse((self.root / 'installation-args').exists())
        self.assertFalse((self.root / 'database-updated').exists())

    def test_update_without_root_never_installs_or_updates(self):
        code, args = self.dependency_setup_script('pkgfile', euid=1000)
        result = self.bash(code, args, input='y\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('root privileges', result.stderr)
        self.assertFalse((self.root / 'installation-args').exists())
        self.assertFalse((self.root / 'database-updated').exists())

    def test_missing_or_incomplete_databases_show_setup_instructions(self):
        deb = self.make_deb()
        for name in CACHE_NAMES + ['pkgfile']:
            with self.subTest(database=name):
                script = self.conversion_script()
                if name == 'pkgfile':
                    (self.root / 'pkgfile-cache/core.files.123').unlink()
                else:
                    (self.root / 'cache' / name).write_text('')
                result = self.bash('bash "$1" -Q "$2"', [script, deb])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Package databases are missing, empty, or incomplete',
                              result.stderr)
                self.assertIn('sudo debtap -u', result.stderr)
                self.assertNotIn('ls:', result.stderr)
                self.assertEqual(list(self.root.glob('*.pkg.tar.zst')), [])

    def test_pkgfile_update_failure_names_the_failed_step(self):
        code = self.update_fixtures() + '\npkgfile() { return 1; }; update_database'
        result = self.bash(code)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Could not update the pkgfile database', result.stderr)
        for name in CACHE_NAMES:
            self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')
        self.assertEqual(list((self.root / 'cache').glob('.update.*')), [])

    def test_failed_updates_preserve_all_caches_and_clean_staging(self):
        code = self.update_fixtures()
        for failure in ('packages.ubuntu.com', 'sid/main', 'sid/non-free', 'sid/contrib',
                        'archive.ubuntu.com', 'github.com', 'aur.archlinux.org'):
            with self.subTest(failure=failure):
                result = self.bash(code + '\nfail=$1; update_database', [failure])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Error: Could not', result.stderr)
                for name in CACHE_NAMES:
                    self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')
                self.assertEqual(list((self.root / 'cache').glob('.update.*')), [])
        (self.root / 'contents.gz').write_bytes(b'invalid gzip')
        self.assertNotEqual(self.bash(code + '\nupdate_database').returncode, 0)
        for name in CACHE_NAMES:
            self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')

    def test_failed_virtual_extraction_and_base_tree_preserve_caches(self):
        code = self.update_fixtures()
        (self.root / 'virtual.tar.gz').write_bytes(b'invalid archive')
        self.assertNotEqual(self.bash(code + '\nupdate_database').returncode, 0)
        for name in CACHE_NAMES:
            self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')
        archive(self.root / 'virtual.tar.gz', {
            'virtual-packages-list-generator-master/virtual-packages': 'virtual-demo\n'})
        self.assertNotEqual(self.bash(code + '\npactree() { return 1; }; update_database').returncode, 0)
        for name in CACHE_NAMES:
            self.assertEqual((self.root / 'cache' / name).read_text(), 'old cache\n')

    def test_successful_update_replaces_all_caches(self):
        self.assert_ok(self.bash(self.update_fixtures() + '\nupdate_database'))
        for name in CACHE_NAMES:
            path = self.root / 'cache' / name
            self.assertNotEqual(path.read_text(), 'old cache\n')
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list((self.root / 'cache').glob('.update.*')), [])


if __name__ == '__main__':
    unittest.main()
