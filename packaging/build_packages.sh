#!/usr/bin/env bash
# Build every release package from the working tree into ./dist:
#   netrex_VER_all.deb, netrex-VER.tar.gz,
#   netrex-VER-1.noarch.rpm, netrex-VER-1-any.pkg.tar.zst
# The packages are named `netrex`; they install into /opt/quota-manager (the
# on-disk path is deliberately unchanged) and replace the old `quota-manager`
# package on upgrade (Arch keeps the pacman package name quota-manager for now,
# only its file is named netrex-*; see packaging/arch/PKGBUILD).
# Used by BOTH the release workflow and the packaging smoke-test workflow, so
# what gets tested is byte-for-byte what gets released.
# Needs: dpkg-deb, tar, zstd, rpmbuild. Usage: packaging/build_packages.sh [workdir]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
VERSION="$(python3 -c "import sys; sys.path.insert(0,'.'); from quota.version import __version__; print(__version__)")"
W="${1:-$(mktemp -d)}"
STAGE="$W/stage"
rm -rf "$STAGE" "$W/tarball" "$W/rpmbuild" "$W/archpkg"
mkdir -p dist "$STAGE/DEBIAN" "$STAGE/opt/quota-manager"

# ---- payload (explicit allowlist) ----
cp run.py requirements-linux.txt LICENSE quota-manager.gpg "$STAGE/opt/quota-manager/"
cp -r core quota api web scripts "$STAGE/opt/quota-manager/"
[ -d bin ] && cp -r bin "$STAGE/opt/quota-manager/" || true
# Normalise modes explicitly: never inherit whatever the checkout/umask gave us.
find "$STAGE/opt/quota-manager" -type d -exec chmod 755 {} +
find "$STAGE/opt/quota-manager" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/opt/quota-manager/scripts"/*.sh
[ -f "$STAGE/opt/quota-manager/bin/sing-box" ] && chmod 755 "$STAGE/opt/quota-manager/bin/sing-box" || true
find "$STAGE/opt/quota-manager" -name __pycache__ -type d -prune -exec rm -rf {} +

# ---- .deb ----
sed -e "s/__VERSION__/${VERSION}/g" -e '/^#/d' packaging/DEBIAN/control.template > "$STAGE/DEBIAN/control"
printf 'netrex (%s) stable; urgency=medium\n\n  * Linux-only metered-internet quota gateway.\n\n -- Netrex Maintainer <noreply@github.com>  Tue, 05 Aug 2026 00:00:00 +0000\n' \
    "${VERSION}" > "$STAGE/DEBIAN/changelog"
cp packaging/DEBIAN/postinst packaging/DEBIAN/prerm "$STAGE/DEBIAN/"
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"
dpkg-deb --build --root-owner-group "$STAGE" "dist/netrex_${VERSION}_all.deb"
# the DEBIAN dir must not leak into the other formats' payload
mv "$STAGE/DEBIAN" "$W/DEBIAN.keep"

# ---- tarball ----
T="$W/tarball/netrex-${VERSION}"
mkdir -p "$T"
cp -a "$STAGE/opt/quota-manager/." "$T/"
cp packaging/generic/install.sh "$T/install.sh"
chmod 755 "$T/install.sh"
if find "$T" -type l | grep -q .; then echo "symlink in tarball payload" >&2; exit 1; fi
tar -C "$W/tarball" --owner=0 --group=0 --numeric-owner \
    -czf "dist/netrex-${VERSION}.tar.gz" "netrex-${VERSION}"

# ---- rpm ----
TOP="$W/rpmbuild"
mkdir -p "$TOP"/{BUILD,RPMS,SPECS,SRPMS}
sed -e "s/__VERSION__/${VERSION}/g" -e "s#__PAYLOAD__#${STAGE}#g" \
    packaging/rpm/netrex.spec.template > "$TOP/SPECS/netrex.spec"
rpmbuild -bb --define "_topdir $TOP" "$TOP/SPECS/netrex.spec" >/dev/null
cp "$TOP/RPMS/noarch/netrex-${VERSION}-1.noarch.rpm" dist/

# ---- arch ----
A="$W/archpkg"
mkdir -p "$A/opt"
cp -a "$STAGE/opt/quota-manager" "$A/opt/"
SIZE="$(du -sb "$A/opt" | cut -f1)"
printf 'pkgname = quota-manager\npkgbase = quota-manager\npkgver = %s-1\npkgdesc = Fair-split metered-internet quota gateway\nurl = https://github.com/UserJoo9/Netrex\nbuilddate = %s\npackager = Netrex CI\nsize = %s\narch = any\nlicense = MIT\ndepend = python>=3.10\ndepend = python-pip\ndepend = dnsmasq\ndepend = nftables\ndepend = iproute2\ndepend = kmod\ndepend = ppp\ndepend = ca-certificates\ndepend = gnupg\n' \
    "${VERSION}" "$(date +%s)" "${SIZE}" > "$A/.PKGINFO"
cp packaging/arch/netrex.install "$A/.INSTALL"
tar -C "$A" --owner=0 --group=0 --numeric-owner --zstd \
    -cf "dist/netrex-${VERSION}-1-any.pkg.tar.zst" .PKGINFO .INSTALL opt

echo "built:"; ls -l dist
