#!/usr/bin/env bash
# Sign the checksums of the four packages for this release. Import the signer
# from stdin in an isolated GnuPG home and verify against the shipped keyring.
set -euo pipefail

version="${1:?usage: sign_release.sh VERSION DIST_DIR PUBLIC_KEYRING}"
dist="$(cd "${2:?missing dist directory}" && pwd)"
keyring="$(cd "$(dirname "${3:?missing public keyring}")" && pwd)/$(basename "$3")"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]

sign_home="$(mktemp -d /tmp/qm-sign.XXXXXX)"
cleanup() {
    gpgconf --homedir "$sign_home" --kill gpg-agent || true
    rm -rf -- "$sign_home"
}
trap cleanup EXIT

cd "$dist"
sha256sum -- \
    "netrex_${version}_all.deb" \
    "netrex-${version}.tar.gz" \
    "netrex-${version}-1.noarch.rpm" \
    "netrex-${version}-1-any.pkg.tar.zst" > "$sign_home/SHA256SUMS"

gpg --homedir "$sign_home" --batch --import
gpg --homedir "$sign_home" --batch --pinentry-mode loopback --passphrase '' \
    --digest-algo SHA256 --detach-sign "$sign_home/SHA256SUMS"
gpgv --homedir "$sign_home" --keyring "$keyring" \
    --weak-digest SHA1 "$sign_home/SHA256SUMS.sig" "$sign_home/SHA256SUMS"
mv -- "$sign_home/SHA256SUMS" "$sign_home/SHA256SUMS.sig" "$dist/"
