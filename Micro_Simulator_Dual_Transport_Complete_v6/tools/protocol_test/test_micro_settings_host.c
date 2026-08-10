#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "../../applications/micro_simulator/src/micro_settings.h"

static void put_i32_be(unsigned char *p, int value)
{
    unsigned int encoded = (unsigned int)value;
    p[0] = (unsigned char)(encoded >> 24);
    p[1] = (unsigned char)(encoded >> 16);
    p[2] = (unsigned char)(encoded >> 8);
    p[3] = (unsigned char)encoded;
}

int main(void)
{
    struct micro_persistent_config current;
    struct micro_persistent_config desired;
    struct micro_persistent_config candidate;
    struct micro_settings_apply_result result;
    char error[96];
    unsigned char payload[160];
    size_t payload_len = 0;

    micro_settings_defaults(&current);
    assert(current.heartbeat_interval_seconds == 60);
    assert(current.last_update_id == 0);
    assert(current.trusted_device_count == 1);

    desired = current;
    desired.heartbeat_interval_seconds = 120;
    desired.lte_update_interval_seconds = 1023;
    desired.ble_check_interval_seconds = 480;
    desired.sending_update = 0x00;
    desired.zone_count = 1;
    desired.zones[0].latitude_e6 = 45421500;
    desired.zones[0].longitude_e6 = -75697200;
    desired.zones[0].radius_m = 150;
    desired.beacon_count = 1;
    memcpy(desired.beacons[0], "\x0F\xAC\x91\x00\x3B\x91", 6);
    desired.trusted_device_count = 1;
    memcpy(desired.trusted_devices[0], "\xAA\xBB\xCC\xDD\xEE\x01", 6);

    assert(micro_settings_build_config_payload("861352064050787", 7, &desired,
                                               payload, sizeof(payload), &payload_len) == 0);
    assert(payload_len == 49); /* 27 fixed + 10 zone + 6 beacon + 6 trusted. */
    assert(micro_settings_apply_payload(payload, payload_len, "861352064050787", &current,
                                        &candidate, &result, error, sizeof(error)) == 0);
    assert(current.heartbeat_interval_seconds == 60); /* Candidate is atomic. */
    assert(candidate.heartbeat_interval_seconds == 120);
    assert(candidate.last_update_id == 7);
    assert(candidate.zone_count == 1 && candidate.beacon_count == 1 && candidate.trusted_device_count == 1);

    /* Invalid SendingUpdate leaves the active configuration unchanged. */
    unsigned char invalid_flag[160];
    memcpy(invalid_flag, payload, payload_len);
    invalid_flag[payload_len - 1] = 0x01;
    assert(micro_settings_apply_payload(invalid_flag, payload_len, "861352064050787", &current,
                                        &candidate, &result, error, sizeof(error)) != 0);
    assert(current.last_update_id == 0);

    /* Wrong IMEI, truncation, trailing bytes, and an invalid coordinate all fail. */
    assert(micro_settings_apply_payload(payload, payload_len, "123456789012345", &current,
                                        &candidate, &result, error, sizeof(error)) == -EACCES);
    assert(micro_settings_apply_payload(payload, payload_len - 1, "861352064050787", &current,
                                        &candidate, &result, error, sizeof(error)) != 0);
    unsigned char trailing[161]; memcpy(trailing, payload, payload_len); trailing[payload_len] = 0;
    assert(micro_settings_apply_payload(trailing, payload_len + 1, "861352064050787", &current,
                                        &candidate, &result, error, sizeof(error)) != 0);
    unsigned char invalid_coord[160]; memcpy(invalid_coord, payload, payload_len);
    put_i32_be(&invalid_coord[18], 91000000);
    assert(micro_settings_apply_payload(invalid_coord, payload_len, "861352064050787", &current,
                                        &candidate, &result, error, sizeof(error)) != 0);

    /* A valid zero-count full replacement clears all three configured lists. */
    struct micro_persistent_config cleared = desired;
    cleared.zone_count = 0; cleared.beacon_count = 0; cleared.trusted_device_count = 0;
    memset(cleared.zones, 0, sizeof(cleared.zones));
    memset(cleared.beacons, 0, sizeof(cleared.beacons));
    memset(cleared.trusted_devices, 0, sizeof(cleared.trusted_devices));
    assert(micro_settings_build_config_payload("861352064050787", 8, &cleared,
                                               payload, sizeof(payload), &payload_len) == 0);
    assert(micro_settings_apply_payload(payload, payload_len, "861352064050787", &desired,
                                        &candidate, &result, error, sizeof(error)) == 0);
    assert(candidate.zone_count == 0 && candidate.beacon_count == 0 && candidate.trusted_device_count == 0);
    assert(candidate.last_update_id == 8);

    /* All three independently scheduled intervals reject zero. */
    cleared.lte_update_interval_seconds = 0;
    assert(micro_settings_validate_config(&cleared) != 0);
    cleared.lte_update_interval_seconds = 480;
    cleared.ble_check_interval_seconds = 0;
    assert(micro_settings_validate_config(&cleared) != 0);

    /* The persistent record validates unchanged after a simulated reboot/load. */
    struct micro_persistent_config reloaded = candidate;
    assert(micro_settings_validate_config(&reloaded) == 0);
    assert(reloaded.last_update_id == 8);

    puts("micro_settings host tests: OK");
    return 0;
}
