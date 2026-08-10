#include "micro_settings.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SAFE_ZONE_RECORD_BYTES 10U
#define CONFIG_FIXED_PAYLOAD_BYTES 27U

static const struct micro_setting_definition registry[] = {
    { MICRO_SETTING_HEARTBEAT_INTERVAL, "heartbeat_interval_seconds" },
    { MICRO_SETTING_LTE_UPDATE_INTERVAL, "lte_update_interval_seconds" },
    { MICRO_SETTING_BLE_CHECK_INTERVAL, "ble_check_interval_seconds" },
    { MICRO_SETTING_SAFE_ZONES, "safe_zones" },
    { MICRO_SETTING_BEACON_LIST, "beacon_list" },
    { MICRO_SETTING_TRUSTED_DEVICE_LIST, "trusted_device_list" },
    { MICRO_SETTING_SENDING_UPDATE, "sending_update" },
};

static uint16_t get_u16_be(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

static int32_t get_i32_be(const uint8_t *p)
{
    return (int32_t)(((uint32_t)p[0] << 24) |
                     ((uint32_t)p[1] << 16) |
                     ((uint32_t)p[2] << 8) |
                     p[3]);
}

static void put_u16_be(uint8_t *p, uint16_t value)
{
    p[0] = (uint8_t)(value >> 8);
    p[1] = (uint8_t)value;
}

static void put_i32_be(uint8_t *p, int32_t value)
{
    uint32_t encoded = (uint32_t)value;
    p[0] = (uint8_t)(encoded >> 24);
    p[1] = (uint8_t)(encoded >> 16);
    p[2] = (uint8_t)(encoded >> 8);
    p[3] = (uint8_t)encoded;
}

static void set_error(char *text, size_t text_len, const char *message)
{
    if (text != NULL && text_len > 0U) {
        snprintf(text, text_len, "%s", message);
    }
}

static bool valid_imei(const char *imei)
{
    if (imei == NULL || strlen(imei) != 15U) {
        return false;
    }
    for (size_t i = 0U; i < 15U; ++i) {
        if (imei[i] < '0' || imei[i] > '9') {
            return false;
        }
    }
    return true;
}

static int parse_hex(const char *text, uint8_t *out, size_t max_len, size_t *out_len)
{
    size_t len = strlen(text);
    if ((len % 2U) != 0U || len / 2U > max_len) {
        return -EINVAL;
    }
    for (size_t i = 0U; i < len / 2U; ++i) {
        char pair[3] = { text[i * 2U], text[i * 2U + 1U], '\0' };
        char *end = NULL;
        unsigned long value = strtoul(pair, &end, 16);
        if (end == pair || *end != '\0' || value > 255UL) {
            return -EINVAL;
        }
        out[i] = (uint8_t)value;
    }
    *out_len = len / 2U;
    return 0;
}

static bool id_is_zero(const uint8_t value[MICRO_DEVICE_ID_BYTES])
{
    for (size_t i = 0U; i < MICRO_DEVICE_ID_BYTES; ++i) {
        if (value[i] != 0U) {
            return false;
        }
    }
    return true;
}

void micro_settings_defaults(struct micro_persistent_config *config)
{
    memset(config, 0, sizeof(*config));
    config->magic = MICRO_SETTINGS_MAGIC;
    config->storage_version = MICRO_SETTINGS_STORAGE_VERSION;
    config->last_update_id = 0U;
    config->heartbeat_interval_seconds = 60U;
    config->lte_update_interval_seconds = 480U;
    config->ble_check_interval_seconds = 480U;
    config->sending_update = 0x00U;
    config->trusted_device_count = 1U;
    const uint8_t default_trusted[MICRO_DEVICE_ID_BYTES] =
        { 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01 };
    memcpy(config->trusted_devices[0], default_trusted, sizeof(default_trusted));
}

int micro_settings_validate_config(const struct micro_persistent_config *config)
{
    if (config == NULL || config->magic != MICRO_SETTINGS_MAGIC ||
        config->storage_version != MICRO_SETTINGS_STORAGE_VERSION) {
        return -EINVAL;
    }
    if (config->heartbeat_interval_seconds == 0U ||
        config->lte_update_interval_seconds == 0U ||
        config->ble_check_interval_seconds == 0U ||
        (config->sending_update != 0x00U && config->sending_update != 0xFFU)) {
        return -ERANGE;
    }
    if (config->zone_count > MICRO_MAX_SAFE_ZONES ||
        config->beacon_count > MICRO_MAX_BEACONS ||
        config->trusted_device_count > MICRO_MAX_TRUSTED_DEVICES) {
        return -ERANGE;
    }
    for (uint8_t i = 0U; i < config->zone_count; ++i) {
        if (config->zones[i].latitude_e6 < -90000000 || config->zones[i].latitude_e6 > 90000000 ||
            config->zones[i].longitude_e6 < -180000000 || config->zones[i].longitude_e6 > 180000000 ||
            config->zones[i].radius_m == 0U) {
            return -ERANGE;
        }
    }
    for (uint8_t i = 0U; i < config->trusted_device_count; ++i) {
        if (id_is_zero(config->trusted_devices[i])) {
            return -EINVAL;
        }
    }
    return 0;
}

const struct micro_setting_definition *micro_setting_by_name(const char *name)
{
    if (name == NULL) {
        return NULL;
    }
    /* Older local CLI scripts may still use this name.  It maps to the same
     * field without ever changing the positional command-0x02 representation. */
    if (strcmp(name, "sleep_interval_seconds") == 0) {
        name = "ble_check_interval_seconds";
    }
    for (size_t i = 0U; i < sizeof(registry) / sizeof(registry[0]); ++i) {
        if (strcmp(registry[i].name, name) == 0) {
            return &registry[i];
        }
    }
    return NULL;
}

const struct micro_setting_definition *micro_setting_by_id(uint8_t id)
{
    for (size_t i = 0U; i < sizeof(registry) / sizeof(registry[0]); ++i) {
        if (registry[i].id == id) {
            return &registry[i];
        }
    }
    return NULL;
}

size_t micro_setting_definition_count(void)
{
    return sizeof(registry) / sizeof(registry[0]);
}

const struct micro_setting_definition *micro_setting_definition_at(size_t index)
{
    return index < micro_setting_definition_count() ? &registry[index] : NULL;
}

int micro_settings_build_config_payload(const char *imei,
                                        uint16_t update_id,
                                        const struct micro_persistent_config *config,
                                        uint8_t *payload,
                                        size_t payload_max,
                                        size_t *payload_len)
{
    if (!valid_imei(imei) || config == NULL || payload == NULL || payload_len == NULL ||
        micro_settings_validate_config(config) != 0) {
        return -EINVAL;
    }
    size_t needed = CONFIG_FIXED_PAYLOAD_BYTES +
                    (size_t)config->zone_count * SAFE_ZONE_RECORD_BYTES +
                    (size_t)config->beacon_count * MICRO_DEVICE_ID_BYTES +
                    (size_t)config->trusted_device_count * MICRO_DEVICE_ID_BYTES;
    if (needed > payload_max) {
        return -ENOSPC;
    }

    size_t offset = 0U;
    memcpy(&payload[offset], imei, 15U); offset += 15U;
    put_u16_be(&payload[offset], update_id); offset += 2U;
    payload[offset++] = config->zone_count;
    for (uint8_t i = 0U; i < config->zone_count; ++i) {
        put_i32_be(&payload[offset], config->zones[i].latitude_e6); offset += 4U;
        put_i32_be(&payload[offset], config->zones[i].longitude_e6); offset += 4U;
        put_u16_be(&payload[offset], config->zones[i].radius_m); offset += 2U;
    }
    payload[offset++] = config->beacon_count;
    for (uint8_t i = 0U; i < config->beacon_count; ++i) {
        memcpy(&payload[offset], config->beacons[i], MICRO_DEVICE_ID_BYTES);
        offset += MICRO_DEVICE_ID_BYTES;
    }
    payload[offset++] = config->trusted_device_count;
    for (uint8_t i = 0U; i < config->trusted_device_count; ++i) {
        memcpy(&payload[offset], config->trusted_devices[i], MICRO_DEVICE_ID_BYTES);
        offset += MICRO_DEVICE_ID_BYTES;
    }
    put_u16_be(&payload[offset], config->heartbeat_interval_seconds); offset += 2U;
    put_u16_be(&payload[offset], config->lte_update_interval_seconds); offset += 2U;
    put_u16_be(&payload[offset], config->ble_check_interval_seconds); offset += 2U;
    payload[offset++] = config->sending_update;
    *payload_len = offset;
    return offset == needed ? 0 : -EIO;
}

static int apply_override(struct micro_persistent_config *candidate,
                          const char *name, const char *value_text)
{
    const struct micro_setting_definition *definition = micro_setting_by_name(name);
    if (definition == NULL || value_text == NULL) {
        return -ENOENT;
    }
    char *end = NULL;
    unsigned long number;
    uint8_t raw[MICRO_MAX_SAFE_ZONES * SAFE_ZONE_RECORD_BYTES];
    size_t raw_len = 0U;

    if (definition->id == MICRO_SETTING_HEARTBEAT_INTERVAL ||
        definition->id == MICRO_SETTING_LTE_UPDATE_INTERVAL ||
        definition->id == MICRO_SETTING_BLE_CHECK_INTERVAL) {
        number = strtoul(value_text, &end, 10);
        if (end == value_text || *end != '\0' || number > UINT16_MAX ||
            (definition->id == MICRO_SETTING_HEARTBEAT_INTERVAL && number == 0U)) {
            return -ERANGE;
        }
        if (definition->id == MICRO_SETTING_HEARTBEAT_INTERVAL) candidate->heartbeat_interval_seconds = (uint16_t)number;
        else if (definition->id == MICRO_SETTING_LTE_UPDATE_INTERVAL) candidate->lte_update_interval_seconds = (uint16_t)number;
        else candidate->ble_check_interval_seconds = (uint16_t)number;
        return 0;
    }
    if (definition->id == MICRO_SETTING_SENDING_UPDATE) {
        number = strtoul(value_text, &end, 0);
        if (end == value_text || *end != '\0' || (number != 0x00U && number != 0xFFU)) {
            return -ERANGE;
        }
        candidate->sending_update = (uint8_t)number;
        return 0;
    }
    if (parse_hex(value_text, raw, sizeof(raw), &raw_len) != 0) {
        return -EINVAL;
    }
    if (definition->id == MICRO_SETTING_SAFE_ZONES) {
        if (raw_len % SAFE_ZONE_RECORD_BYTES != 0U || raw_len / SAFE_ZONE_RECORD_BYTES > MICRO_MAX_SAFE_ZONES) {
            return -ERANGE;
        }
        candidate->zone_count = (uint8_t)(raw_len / SAFE_ZONE_RECORD_BYTES);
        memset(candidate->zones, 0, sizeof(candidate->zones));
        for (uint8_t i = 0U; i < candidate->zone_count; ++i) {
            size_t offset = (size_t)i * SAFE_ZONE_RECORD_BYTES;
            candidate->zones[i].latitude_e6 = get_i32_be(&raw[offset]);
            candidate->zones[i].longitude_e6 = get_i32_be(&raw[offset + 4U]);
            candidate->zones[i].radius_m = get_u16_be(&raw[offset + 8U]);
        }
        return 0;
    }
    if (raw_len % MICRO_DEVICE_ID_BYTES != 0U || raw_len / MICRO_DEVICE_ID_BYTES > MICRO_MAX_BEACONS) {
        return -ERANGE;
    }
    uint8_t count = (uint8_t)(raw_len / MICRO_DEVICE_ID_BYTES);
    if (definition->id == MICRO_SETTING_BEACON_LIST) {
        candidate->beacon_count = count;
        memset(candidate->beacons, 0, sizeof(candidate->beacons));
        memcpy(candidate->beacons, raw, raw_len);
    } else {
        candidate->trusted_device_count = count;
        memset(candidate->trusted_devices, 0, sizeof(candidate->trusted_devices));
        memcpy(candidate->trusted_devices, raw, raw_len);
    }
    return 0;
}

int micro_settings_build_config_with_override_payload(
    const char *imei, uint16_t update_id, const struct micro_persistent_config *current,
    const char *name, const char *value_text, uint8_t *payload,
    size_t payload_max, size_t *payload_len)
{
    if (current == NULL || micro_settings_validate_config(current) != 0) {
        return -EINVAL;
    }
    struct micro_persistent_config candidate = *current;
    int err = apply_override(&candidate, name, value_text);
    if (err != 0 || micro_settings_validate_config(&candidate) != 0) {
        return err != 0 ? err : -ERANGE;
    }
    return micro_settings_build_config_payload(imei, update_id, &candidate,
                                               payload, payload_max, payload_len);
}

static void record_changed(uint8_t id, bool changed,
                           struct micro_settings_apply_result *result)
{
    if (changed && result->changed_count < sizeof(result->changed_ids)) {
        result->changed_ids[result->changed_count++] = id;
    }
}

int micro_settings_apply_payload(const uint8_t *payload,
                                 size_t payload_len,
                                 const char *expected_imei,
                                 const struct micro_persistent_config *current,
                                 struct micro_persistent_config *candidate,
                                 struct micro_settings_apply_result *result,
                                 char *error_text,
                                 size_t error_text_len)
{
    if (payload == NULL || current == NULL || candidate == NULL || result == NULL ||
        payload_len < CONFIG_FIXED_PAYLOAD_BYTES || !valid_imei(expected_imei) ||
        micro_settings_validate_config(current) != 0) {
        set_error(error_text, error_text_len, "invalid configuration-update payload");
        return -EINVAL;
    }
    if (memcmp(payload, expected_imei, 15U) != 0) {
        set_error(error_text, error_text_len, "target IMEI mismatch");
        return -EACCES;
    }

    struct micro_persistent_config next = *current;
    size_t offset = 15U;
    uint16_t update_id = get_u16_be(&payload[offset]); offset += 2U;
    uint8_t zone_count = payload[offset++];
    if (zone_count > MICRO_MAX_SAFE_ZONES) {
        set_error(error_text, error_text_len, "safe-zone count exceeds four");
        return -ERANGE;
    }
    if (offset + (size_t)zone_count * SAFE_ZONE_RECORD_BYTES + 1U > payload_len) {
        set_error(error_text, error_text_len, "truncated safe-zone record");
        return -EMSGSIZE;
    }
    next.zone_count = zone_count;
    memset(next.zones, 0, sizeof(next.zones));
    for (uint8_t i = 0U; i < zone_count; ++i) {
        next.zones[i].latitude_e6 = get_i32_be(&payload[offset]); offset += 4U;
        next.zones[i].longitude_e6 = get_i32_be(&payload[offset]); offset += 4U;
        next.zones[i].radius_m = get_u16_be(&payload[offset]); offset += 2U;
    }
    uint8_t beacon_count = payload[offset++];
    if (beacon_count > MICRO_MAX_BEACONS) {
        set_error(error_text, error_text_len, "beacon count exceeds four");
        return -ERANGE;
    }
    if (offset + (size_t)beacon_count * MICRO_DEVICE_ID_BYTES + 1U > payload_len) {
        set_error(error_text, error_text_len, "truncated beacon record");
        return -EMSGSIZE;
    }
    next.beacon_count = beacon_count;
    memset(next.beacons, 0, sizeof(next.beacons));
    for (uint8_t i = 0U; i < beacon_count; ++i) {
        memcpy(next.beacons[i], &payload[offset], MICRO_DEVICE_ID_BYTES);
        offset += MICRO_DEVICE_ID_BYTES;
    }
    uint8_t trusted_count = payload[offset++];
    if (trusted_count > MICRO_MAX_TRUSTED_DEVICES) {
        set_error(error_text, error_text_len, "trusted-device count exceeds four");
        return -ERANGE;
    }
    if (offset + (size_t)trusted_count * MICRO_DEVICE_ID_BYTES + 7U > payload_len) {
        set_error(error_text, error_text_len, "truncated trusted-device record");
        return -EMSGSIZE;
    }
    next.trusted_device_count = trusted_count;
    memset(next.trusted_devices, 0, sizeof(next.trusted_devices));
    for (uint8_t i = 0U; i < trusted_count; ++i) {
        memcpy(next.trusted_devices[i], &payload[offset], MICRO_DEVICE_ID_BYTES);
        offset += MICRO_DEVICE_ID_BYTES;
    }
    next.heartbeat_interval_seconds = get_u16_be(&payload[offset]); offset += 2U;
    next.lte_update_interval_seconds = get_u16_be(&payload[offset]); offset += 2U;
    next.ble_check_interval_seconds = get_u16_be(&payload[offset]); offset += 2U;
    next.sending_update = payload[offset++];
    if (offset != payload_len) {
        set_error(error_text, error_text_len, "unexpected trailing payload bytes");
        return -EMSGSIZE;
    }
    next.last_update_id = update_id;
    if (micro_settings_validate_config(&next) != 0) {
        set_error(error_text, error_text_len, "configuration value out of range");
        return -ERANGE;
    }

    memset(result, 0, sizeof(*result));
    result->update_id = update_id;
    record_changed(MICRO_SETTING_HEARTBEAT_INTERVAL,
                   current->heartbeat_interval_seconds != next.heartbeat_interval_seconds, result);
    record_changed(MICRO_SETTING_LTE_UPDATE_INTERVAL,
                   current->lte_update_interval_seconds != next.lte_update_interval_seconds, result);
    record_changed(MICRO_SETTING_BLE_CHECK_INTERVAL,
                   current->ble_check_interval_seconds != next.ble_check_interval_seconds, result);
    record_changed(MICRO_SETTING_SAFE_ZONES,
                   current->zone_count != next.zone_count ||
                   memcmp(current->zones, next.zones, sizeof(next.zones)) != 0, result);
    record_changed(MICRO_SETTING_BEACON_LIST,
                   current->beacon_count != next.beacon_count ||
                   memcmp(current->beacons, next.beacons, sizeof(next.beacons)) != 0, result);
    record_changed(MICRO_SETTING_TRUSTED_DEVICE_LIST,
                   current->trusted_device_count != next.trusted_device_count ||
                   memcmp(current->trusted_devices, next.trusted_devices,
                          sizeof(next.trusted_devices)) != 0, result);
    record_changed(MICRO_SETTING_SENDING_UPDATE,
                   current->sending_update != next.sending_update, result);
    *candidate = next;
    return 0;
}
