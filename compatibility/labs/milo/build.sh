#!/usr/bin/env sh
# Build Eclipse Milo's example server from Milo's own source, unmodified apart
# from one build-file line, and print the classpath the conformance config's
# MILO_CLASSPATH wants. Nothing here is shipped.
#
#   compatibility/labs/milo/build.sh [<work-dir>]
#   export MILO_CLASSPATH="$(cat <work-dir>/classpath)"
#
# Needs git, a JDK 17+ and Maven. Only the examples module is built; the Milo
# stack itself comes from Maven Central at the same version.
set -eu

VERSION="${MILO_VERSION:-v1.1.7}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
WORK="${1:-$ROOT/.conformance/milo}"

mkdir -p "$WORK"
if [ ! -d "$WORK/src" ]; then
  git clone -q --depth 1 --branch "$VERSION" https://github.com/eclipse-milo/milo.git "$WORK/src"
  # Built on its own, outside Milo's reactor, the examples module no longer sees
  # Guava through a sibling module, though two example classes import it.
  # Declaring it is the only change; the server code is untouched.
  python3 - "$WORK/src/milo-examples/server-examples/pom.xml" <<'EOF'
import sys
path = sys.argv[1]
pom = open(path).read()
marker = "    <dependency>\n      <groupId>org.slf4j</groupId>"
guava = (
    "    <dependency>\n      <groupId>com.google.guava</groupId>\n"
    "      <artifactId>guava</artifactId>\n      <version>${guava.version}</version>\n"
    "    </dependency>\n"
)
if "com.google.guava" not in pom:
    open(path, "w").write(pom.replace(marker, guava + marker, 1))
EOF
fi

POM="$WORK/src/milo-examples/server-examples/pom.xml"
mvn -q -B -f "$POM" -DskipTests package
mvn -q -B -f "$POM" dependency:build-classpath -Dmdep.outputFile="$WORK/deps"
# The unshaded jar plus its dependencies: the shaded one leaves out part of the
# Milo stack when built outside the reactor.
JAR="$(ls "$WORK"/src/milo-examples/server-examples/target/original-milo-server-examples-*.jar)"
printf '%s:%s\n' "$JAR" "$(cat "$WORK/deps")" > "$WORK/classpath"
echo "$WORK/classpath"
