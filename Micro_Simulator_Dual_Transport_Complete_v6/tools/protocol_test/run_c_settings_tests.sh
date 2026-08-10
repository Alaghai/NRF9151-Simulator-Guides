#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cc -std=c11 -Wall -Wextra -Werror \
  -I"$ROOT/applications/micro_simulator/src" \
  "$ROOT/tools/protocol_test/test_micro_settings_host.c" \
  "$ROOT/applications/micro_simulator/src/micro_settings.c" \
  -o /tmp/test_micro_settings_host
/tmp/test_micro_settings_host
