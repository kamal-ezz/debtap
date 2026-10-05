debtap
======

A script for converting .deb packages into Arch Linux packages, focused on accuracy

# FAQ

**Q: What "debtap" stands for?**

**A:** DEB To Arch (Linux) Package

**Q: Isn't better to download an official package or write a PKGBUILD in case I need to compile a package or convert a .deb package to an Arch Linux package?**

**A:** Sure it is, and I truely encourage you to do so. Debtap was written to create packages that either cannot be compiled (closed source packages) or cannot be built from AUR for various reasons (error during compiling or unavailable files), as a quick 'n' dirty solution and an extra option for creating Arch Linux packages for Arch Linux users.

**Q: So debtap will help me only in case I need to convert specific .deb packages to Arch Linux packages?**

**A:** No. In case you need to write a new PKGBUILD for a package that already exists in the Debian/Ubuntu distributions, using parameter -p or -P it can generate a PKGBUILD and then edit it as you wish.

**Q: What are the minimum requirements to run this script?**

**A:** Run `sudo debtap -u` at least once (preferably recently) to create/update
the pkgfile and debtap databases. This checks the commands required for updating
and conversion, lists any missing commands and their Arch packages, and offers
to install them using `pacman -S --needed`. Installation requires your approval
and keeps pacman's normal confirmation prompt. Declining, providing no input,
or a failed installation stops the update. Quiet options do not bypass this
installation prompt. Conversion itself does not install packages.

**Q: Debtap needs a lot of time to convert a package. So, why this is happening?**

**A:** Like I said, debtap is focused on accuracy. It won't just unpack a .deb package and then repackage its data to an Arch Linux package, ignoring metadata. Depending on the speed of your processor and the package itself, conversion can take from a few seconds to several minutes.

**Q: During conversion I get several warning messages, why?**

**A:** Debtap cannot be 100% accurate for several reasons,  the main reason for this is the complexity of packages names. If you want to check the freshly generated `.PKGINFO` and `.INSTALL` (this is optional file) metadata files or even fix the untranslated packages names inside `.PKGINFO`, debtap offers you the option to edit these files before compressing the final package.

**Q: How do I use debtap?**

**A:** The syntax is quite simple actually: `Syntax: debtap -o output_directory [other_options] package_filename`

For example: `debtap world-of-goo-demo_1.0_i386.deb`

When generating a PKGBUILD, debtap preserves an existing `<pkgname>-PKGBUILD`
directory and exits with an error. Choose another output directory or rename the
existing directory before generating it again.

Conversion uses a private temporary directory under `${TMPDIR:-/tmp}` and removes
it on completion, failure, or interruption. Extracted package contents are kept
separate from conversion metadata and scratch files.

Reconversion replaces an existing output package with the same filename. The
complete compressed package is staged on the output filesystem, then published
with an atomic rename. A failed conversion preserves the previous package.
If the destination is a symlink, the symlink is replaced and its target is
preserved. Archive, compression, or publication failures return a nonzero exit
status.

Maintainer scripts are preserved as shell code inside isolated `.INSTALL`
functions, with Debian action arguments for installation, configuration,
upgrade, and removal. Debtap checks shell syntax before conversion and again
after metadata editing. Non-shell interpreters require manual adaptation.
Scripts are not executed during conversion.

Review `.INSTALL` for Arch compatibility before installing. Commands that
configure APT repositories, use debconf, or assume Debian services are preserved
and flagged for review; syntax validation does not make them portable. Debian
purge, rollback, triggers, and old-package upgrade removal scripts are not
emulated. Arch's transaction hooks handle desktop and icon cache updates.

Any recommendations or questions for debtap are welcomed!

Available options:
==================

    -h  --help        Prints help
    -u  --update      Check dependencies and update databases
    -q  --quiet       Bypass all questions, except for editing metadata file(s)
    -Q  --Quiet       Bypass all questions (not recommended)
    -s  --pseudo      Create a pseudo-64-bit package from a 32-bit .deb package
    -w  --wipeout     Wipeout versions from all dependencies, conflicts etc.
    -p  --pkgbuild    Additionally generate a PKGBUILD file
    -P  --Pkgbuild    Generate a PKGBUILD file only
    -o  --output      Output directory for generated package and/or PKGBUILD (optional)
    -v  --version     Print version

ChatGPT recovery helper:
========================

After pulling the fixed debtap version, run as your normal desktop user:

    ./scripts/reinstall-chatgpt.sh --build-only ~/Downloads/chatgpt_amd64.deb
    ./scripts/reinstall-chatgpt.sh ~/Downloads/chatgpt_amd64.deb

The first command only builds a package for inspection. The second rebuilds,
removes the reviewed APT setup, installs through sudo/pacman with its normal
confirmation, restores Firefox for HTTP/HTTPS, and launches ChatGPT. It retains
the package, PKGBUILD, cleaned install script, logs, and desktop association
backups in the printed temporary directory. Existing desktop preferences are
changed only after installation succeeds.

The adaptation recognizes the exact maintainer scripts from ChatGPT
26.930.51102 and refuses changed scripts. AppArmor setup and removal are
preserved. Future releases require a fresh review. Firefox must already be
installed; set `FIREFOX_DESKTOP` if its desktop entry has another filename.
The helper does not commit or push repository changes.

Development checks:
===================

    bash -n debtap
    python3 -m unittest discover -s tests -v

The regression suite uses temporary package fixtures and mocked repository
lookups; it needs no root access or network. It requires Python 3 and the local
conversion tools, including GNU tar, bsdtar, fakeroot, zstd, ar, and gawk.
