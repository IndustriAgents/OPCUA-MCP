#!/usr/bin/env sh
# Build open62541 from source with the features the conformance lab server
# uses, then the lab server against it. Nothing here is shipped.
#
#   compatibility/labs/open62541/build.sh [<work-dir>]
#
# Needs git, cmake, a C compiler and OpenSSL 3 headers. The binary lands at
# <work-dir>/lab_server (default: .conformance/open62541 in the repo root).
set -eu

VERSION="${OPEN62541_VERSION:-v1.5.8}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
WORK="${1:-$ROOT/.conformance/open62541}"
JOBS="${JOBS:-4}"

mkdir -p "$WORK"
if [ ! -d "$WORK/src" ]; then
  # Full namespace 0 (needed for Alarms & Conditions) lives in a submodule.
  git clone -q --depth 1 --branch "$VERSION" --recurse-submodules --shallow-submodules \
    https://github.com/open62541/open62541.git "$WORK/src"
fi

OPENSSL_ARGS=""
if command -v brew >/dev/null 2>&1 && brew --prefix openssl@3 >/dev/null 2>&1; then
  OPENSSL_ARGS="-DOPENSSL_ROOT_DIR=$(brew --prefix openssl@3)"
fi

if [ ! -f "$WORK/install/lib/libopen62541.a" ]; then
  # shellcheck disable=SC2086
  cmake -S "$WORK/src" -B "$WORK/build" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF -DUA_BUILD_EXAMPLES=OFF \
    -DUA_ENABLE_ENCRYPTION=OPENSSL $OPENSSL_ARGS \
    -DUA_ENABLE_HISTORIZING=ON -DUA_ENABLE_SUBSCRIPTIONS_EVENTS=ON \
    -DUA_ENABLE_SUBSCRIPTIONS_ALARMS_CONDITIONS=ON -DUA_NAMESPACE_ZERO=FULL \
    -DCMAKE_INSTALL_PREFIX="$WORK/install" >"$WORK/cmake.log"
  cmake --build "$WORK/build" -j "$JOBS" >"$WORK/build.log"
  cmake --install "$WORK/build" >/dev/null
fi

SSL_INC=""
SSL_LIB=""
if [ -n "$OPENSSL_ARGS" ]; then
  SSL_INC="-I$(brew --prefix openssl@3)/include"
  SSL_LIB="-L$(brew --prefix openssl@3)/lib"
fi
LIBDIR="$WORK/install/lib"
[ -d "$LIBDIR" ] || LIBDIR="$WORK/install/lib64"
# shellcheck disable=SC2086
cc -O2 -std=c99 -I"$WORK/install/include" $SSL_INC "$HERE/lab_server.c" \
  -o "$WORK/lab_server" "$LIBDIR/libopen62541.a" $SSL_LIB -lssl -lcrypto -lpthread -lm
echo "$WORK/lab_server"
