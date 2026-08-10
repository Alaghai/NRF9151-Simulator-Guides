#include <assert.h>
#include <errno.h>
#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "../../applications/micro_simulator/src/micro_settings.h"

static void put_u16_be(unsigned char *p, unsigned short value)
{
    p[0] = (unsigned char)(value >> 8);
    p[1] = (unsigned char)value;
}

int main(void)
{
    struct micro_persistent_config current;
    struct micro_persistent_config candidate;
    struct micro_settings_apply_result result;
    char error[96];
    unsigned char payload[128];
    size_t payload_len = 0;

    micro_settings_defaults(&current);
    assert(current.heartbeat_interval_seconds == 60);
    assert(current.trusted_device_count == 1);
    assert(memcmp(current.trusted_devices[0], "\xAA\xBB\xCC\xDD\xEE\x01", 6) == 0);

    assert(micro_settings_build_single_payload(
               "861352064050787", 7, "heartbeat_interval_seconds", "120",
               payload, sizeof(payload), &payload_len) == 0);
    assert(micro_settings_apply_payload(
               payload, payload_len, "861352064050787", &current, &candidate,
               &result, error, sizeof(error)) == 0);
    assert(candidate.heartbeat_interval_seconds == 120);
    assert(current.heartbeat_interval_seconds == 60); /* atomic candidate */
    assert(result.update_id == 7);

    /* Two entries: first valid, second unknown. The current config must remain unchanged. */
    size_t o = 0;
    memcpy(payload + o, "861352064050787", 15); o += 15;
    payload[o++] = MICRO_SETTINGS_SCHEMA_VERSION;
    put_u16_be(payload + o, 8); o += 2;
    payload[o++] = 2;
    payload[o++] = MICRO_SETTING_HEARTBEAT_INTERVAL;
    payload[o++] = MICRO_VALUE_UINT16;
    put_u16_be(payload + o, 2); o += 2;
    put_u16_be(payload + o, 300); o += 2;
    payload[o++] = 0xFE;
    payload[o++] = MICRO_VALUE_UINT16;
    put_u16_be(payload + o, 2); o += 2;
    put_u16_be(payload + o, 1); o += 2;
    assert(micro_settings_apply_payload(
               payload, o, "861352064050787", &current, &candidate,
               &result, error, sizeof(error)) == -ENOENT);
    assert(current.heartbeat_interval_seconds == 60);

    assert(micro_settings_build_single_payload(
               "861352064050787", 9, "trusted_device_list",
               "123456789123AABBCCDDEE02",
               payload, sizeof(payload), &payload_len) == 0);
    assert(micro_settings_apply_payload(
               payload, payload_len, "861352064050787", &current, &candidate,
               &result, error, sizeof(error)) == 0);
    assert(candidate.trusted_device_count == 2);
    assert(memcmp(candidate.trusted_devices[0], "\x12\x34\x56\x78\x91\x23", 6) == 0);

    assert(micro_settings_build_single_payload(
               "861352064050787", 11, "trusted_device_list",
               "000000000000", payload, sizeof(payload), &payload_len) == -EINVAL);

    struct micro_persistent_config invalid_config = current;
    memset(invalid_config.trusted_devices[0], 0, MICRO_DEVICE_ID_BYTES);
    assert(micro_settings_validate_config(&invalid_config) != 0);

    size_t zero_o = 0;
    memcpy(payload + zero_o, "861352064050787", 15); zero_o += 15;
    payload[zero_o++] = MICRO_SETTINGS_SCHEMA_VERSION;
    put_u16_be(payload + zero_o, 12); zero_o += 2;
    payload[zero_o++] = 1;
    payload[zero_o++] = MICRO_SETTING_TRUSTED_DEVICE_LIST;
    payload[zero_o++] = MICRO_VALUE_RAW;
    put_u16_be(payload + zero_o, 6); zero_o += 2;
    memset(payload + zero_o, 0, 6); zero_o += 6;
    assert(micro_settings_apply_payload(
               payload, zero_o, "861352064050787", &current, &candidate,
               &result, error, sizeof(error)) != 0);
    assert(current.trusted_device_count == 1);
    assert(memcmp(current.trusted_devices[0], "\xAA\xBB\xCC\xDD\xEE\x01", 6) == 0);

    assert(micro_settings_apply_payload(
               payload, payload_len, "123456789012345", &current, &candidate,
               &result, error, sizeof(error)) == -EACCES);

    /* A complete defaults packet supports config reset through the same parser. */
    struct micro_persistent_config modified = current;
    modified.heartbeat_interval_seconds = 321;
    modified.lte_update_interval_seconds = 123;
    modified.sleep_interval_seconds = 456;
    modified.zone_count = 1;
    modified.zones[0].latitude_e7 = 454215000;
    modified.zones[0].longitude_e7 = -756972000;
    modified.zones[0].radius_m = 150;
    assert(micro_settings_build_config_payload(
               "861352064050787", 10, &current, payload, sizeof(payload),
               &payload_len) == 0);
    assert(micro_settings_apply_payload(
               payload, payload_len, "861352064050787", &modified, &candidate,
               &result, error, sizeof(error)) == 0);
    assert(candidate.heartbeat_interval_seconds == 60);
    assert(candidate.lte_update_interval_seconds == 480);
    assert(candidate.sleep_interval_seconds == 480);
    assert(candidate.zone_count == 0);
    assert(result.changed_count >= 4);

    /* Re-applying the same payload is valid but records no changed settings. */
    assert(micro_settings_apply_payload(
               payload, payload_len, "861352064050787", &candidate, &current,
               &result, error, sizeof(error)) == 0);
    assert(result.changed_count == 0);

    puts("micro_settings host tests: OK");
    return 0;
}
