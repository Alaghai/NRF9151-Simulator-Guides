#include "micro_settings.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SAFE_ZONE_RECORD_BYTES 10U
#define MAX_TLV_ENTRIES 16U

/* Settings operations are serialized by the application transport mutex.
 * Keep parser/build scratch storage out of the heartbeat/main call frames;
 * this matters when a SUP response is parsed by the micro_heartbeat thread.
 */
struct micro_settings_scratch {
    uint8_t value[64];
    uint8_t zones[MICRO_MAX_SAFE_ZONES * SAFE_ZONE_RECORD_BYTES];
    bool seen[256];
    struct micro_persistent_config before_entry;
};

static struct micro_settings_scratch settings_scratch;

static const struct micro_setting_definition registry[] = {
    { MICRO_SETTING_HEARTBEAT_INTERVAL, "heartbeat_interval_seconds", MICRO_VALUE_UINT16, 2U, 1U, 65535U, 60U, true },
    { MICRO_SETTING_LTE_UPDATE_INTERVAL, "lte_update_interval_seconds", MICRO_VALUE_UINT16, 2U, 1U, 65535U, 480U, true },
    { MICRO_SETTING_SLEEP_INTERVAL, "sleep_interval_seconds", MICRO_VALUE_UINT16, 2U, 1U, 65535U, 480U, true },
    { MICRO_SETTING_SAFE_ZONES, "safe_zones", MICRO_VALUE_RAW, 0U, 0U, MICRO_MAX_SAFE_ZONES * SAFE_ZONE_RECORD_BYTES, 0U, true },
    { MICRO_SETTING_BEACON_LIST, "beacon_list", MICRO_VALUE_RAW, 0U, 0U, MICRO_MAX_BEACONS * MICRO_DEVICE_ID_BYTES, 0U, true },
    { MICRO_SETTING_TRUSTED_DEVICE_LIST, "trusted_device_list", MICRO_VALUE_RAW, 0U, 0U, MICRO_MAX_TRUSTED_DEVICES * MICRO_DEVICE_ID_BYTES, 0U, true },
};

static uint16_t get_u16_be(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

static int32_t get_i32_be(const uint8_t *p)
{
    uint32_t value = ((uint32_t)p[0] << 24) |
                     ((uint32_t)p[1] << 16) |
                     ((uint32_t)p[2] << 8) |
                     p[3];
    return (int32_t)value;
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

static int append_tlv(uint8_t *payload, size_t payload_max, size_t *offset,
                      uint8_t id, uint8_t type,
                      const uint8_t *value, uint16_t value_len)
{
    if (*offset + 4U + value_len > payload_max) {
        return -ENOSPC;
    }
    payload[(*offset)++] = id;
    payload[(*offset)++] = type;
    put_u16_be(&payload[*offset], value_len);
    *offset += 2U;
    if (value_len > 0U) {
        memcpy(&payload[*offset], value, value_len);
        *offset += value_len;
    }
    return 0;
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

void micro_settings_defaults(struct micro_persistent_config *config)
{
    memset(config, 0, sizeof(*config));
    config->magic = MICRO_SETTINGS_MAGIC;
    config->storage_version = MICRO_SETTINGS_STORAGE_VERSION;
    config->heartbeat_interval_seconds = 60U;
    config->lte_update_interval_seconds = 480U;
    config->sleep_interval_seconds = 480U;
    config->trusted_device_count = 1U;
    const uint8_t default_trusted[MICRO_DEVICE_ID_BYTES] = { 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01 };
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
        config->sleep_interval_seconds == 0U) {
        return -ERANGE;
    }
    if (config->zone_count > MICRO_MAX_SAFE_ZONES ||
        config->beacon_count > MICRO_MAX_BEACONS ||
        config->trusted_device_count > MICRO_MAX_TRUSTED_DEVICES) {
        return -ERANGE;
    }
    for (uint8_t i = 0U; i < config->zone_count; ++i) {
        if (config->zones[i].latitude_e7 < -900000000 || config->zones[i].latitude_e7 > 900000000 ||
            config->zones[i].longitude_e7 < -1800000000 || config->zones[i].longitude_e7 > 1800000000 ||
            config->zones[i].radius_m == 0U) {
            return -ERANGE;
        }
    }
    for (uint8_t i = 0U; i < config->trusted_device_count; ++i) {
        bool all_zero = true;
        for (size_t j = 0U; j < MICRO_DEVICE_ID_BYTES; ++j) {
            if (config->trusted_devices[i][j] != 0U) {
                all_zero = false;
                break;
            }
        }
        if (all_zero) {
            return -EINVAL;
        }
    }
    return 0;
}

const struct micro_setting_definition *micro_setting_by_name(const char *name)
{
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

int micro_settings_build_single_payload(const char *imei,
                                        uint16_t update_id,
                                        const char *name,
                                        const char *value_text,
                                        uint8_t *payload,
                                        size_t payload_max,
                                        size_t *payload_len)
{
    const struct micro_setting_definition *definition = micro_setting_by_name(name);
    if (!valid_imei(imei) || definition == NULL || payload == NULL || payload_len == NULL) {
        return -EINVAL;
    }
    size_t value_len = 0U;
    if (definition->value_type == MICRO_VALUE_UINT16) {
        char *end = NULL;
        unsigned long parsed = strtoul(value_text, &end, 10);
        if (end == value_text || *end != '\0' || parsed < definition->minimum || parsed > definition->maximum) {
            return -ERANGE;
        }
        put_u16_be(settings_scratch.value, (uint16_t)parsed);
        value_len = 2U;
    } else if (definition->value_type == MICRO_VALUE_RAW) {
        int err = parse_hex(value_text, settings_scratch.value,
                            sizeof(settings_scratch.value), &value_len);
        if (err != 0) {
            return err;
        }
        if (value_len > definition->maximum) {
            return -ERANGE;
        }
        if (definition->id == MICRO_SETTING_SAFE_ZONES && value_len % SAFE_ZONE_RECORD_BYTES != 0U) {
            return -EINVAL;
        }
        if ((definition->id == MICRO_SETTING_BEACON_LIST || definition->id == MICRO_SETTING_TRUSTED_DEVICE_LIST) &&
            value_len % MICRO_DEVICE_ID_BYTES != 0U) {
            return -EINVAL;
        }
        if (definition->id == MICRO_SETTING_TRUSTED_DEVICE_LIST) {
            for (size_t offset = 0U; offset < value_len; offset += MICRO_DEVICE_ID_BYTES) {
                bool all_zero = true;
                for (size_t j = 0U; j < MICRO_DEVICE_ID_BYTES; ++j) {
                    if (settings_scratch.value[offset + j] != 0U) {
                        all_zero = false;
                        break;
                    }
                }
                if (all_zero) {
                    return -EINVAL;
                }
            }
        }
    } else {
        return -ENOTSUP;
    }

    size_t needed = 15U + 1U + 2U + 1U + 4U + value_len;
    if (needed > payload_max) {
        return -ENOSPC;
    }
    size_t offset = 0U;
    memcpy(&payload[offset], imei, 15U); offset += 15U;
    payload[offset++] = MICRO_SETTINGS_SCHEMA_VERSION;
    put_u16_be(&payload[offset], update_id); offset += 2U;
    payload[offset++] = 1U;
    payload[offset++] = definition->id;
    payload[offset++] = definition->value_type;
    put_u16_be(&payload[offset], (uint16_t)value_len); offset += 2U;
    memcpy(&payload[offset], settings_scratch.value, value_len); offset += value_len;
    *payload_len = offset;
    return 0;
}

int micro_settings_build_config_payload(const char *imei,
                                        uint16_t update_id,
                                        const struct micro_persistent_config *config,
                                        uint8_t *payload,
                                        size_t payload_max,
                                        size_t *payload_len)
{
    if (!valid_imei(imei) || config == NULL || payload == NULL ||
        payload_len == NULL || micro_settings_validate_config(config) != 0) {
        return -EINVAL;
    }
    if (payload_max < 19U) {
        return -ENOSPC;
    }

    size_t offset = 0U;
    memcpy(&payload[offset], imei, 15U); offset += 15U;
    payload[offset++] = MICRO_SETTINGS_SCHEMA_VERSION;
    put_u16_be(&payload[offset], update_id); offset += 2U;
    payload[offset++] = 6U;

    uint8_t numeric[2];
    int err;
    put_u16_be(numeric, config->heartbeat_interval_seconds);
    err = append_tlv(payload, payload_max, &offset,
                     MICRO_SETTING_HEARTBEAT_INTERVAL, MICRO_VALUE_UINT16,
                     numeric, sizeof(numeric));
    if (err != 0) return err;

    put_u16_be(numeric, config->lte_update_interval_seconds);
    err = append_tlv(payload, payload_max, &offset,
                     MICRO_SETTING_LTE_UPDATE_INTERVAL, MICRO_VALUE_UINT16,
                     numeric, sizeof(numeric));
    if (err != 0) return err;

    put_u16_be(numeric, config->sleep_interval_seconds);
    err = append_tlv(payload, payload_max, &offset,
                     MICRO_SETTING_SLEEP_INTERVAL, MICRO_VALUE_UINT16,
                     numeric, sizeof(numeric));
    if (err != 0) return err;

    size_t zones_len = 0U;
    for (uint8_t i = 0U; i < config->zone_count; ++i) {
        put_i32_be(&settings_scratch.zones[zones_len], config->zones[i].latitude_e7);
        zones_len += 4U;
        put_i32_be(&settings_scratch.zones[zones_len], config->zones[i].longitude_e7);
        zones_len += 4U;
        put_u16_be(&settings_scratch.zones[zones_len], config->zones[i].radius_m);
        zones_len += 2U;
    }
    err = append_tlv(payload, payload_max, &offset,
                     MICRO_SETTING_SAFE_ZONES, MICRO_VALUE_RAW,
                      settings_scratch.zones, (uint16_t)zones_len);
    if (err != 0) return err;

    uint16_t beacon_len = (uint16_t)(config->beacon_count * MICRO_DEVICE_ID_BYTES);
    err = append_tlv(payload, payload_max, &offset,
                     MICRO_SETTING_BEACON_LIST, MICRO_VALUE_RAW,
                     &config->beacons[0][0], beacon_len);
    if (err != 0) return err;

    uint16_t trusted_len = (uint16_t)(config->trusted_device_count * MICRO_DEVICE_ID_BYTES);
    err = append_tlv(payload, payload_max, &offset,
                     MICRO_SETTING_TRUSTED_DEVICE_LIST, MICRO_VALUE_RAW,
                     &config->trusted_devices[0][0], trusted_len);
    if (err != 0) return err;

    *payload_len = offset;
    return 0;
}

static int apply_raw_setting(uint8_t id, const uint8_t *value, uint16_t value_len,
                             struct micro_persistent_config *candidate)
{
    if (id == MICRO_SETTING_SAFE_ZONES) {
        if (value_len % SAFE_ZONE_RECORD_BYTES != 0U || value_len / SAFE_ZONE_RECORD_BYTES > MICRO_MAX_SAFE_ZONES) {
            return -EINVAL;
        }
        candidate->zone_count = (uint8_t)(value_len / SAFE_ZONE_RECORD_BYTES);
        memset(candidate->zones, 0, sizeof(candidate->zones));
        size_t offset = 0U;
        for (uint8_t i = 0U; i < candidate->zone_count; ++i) {
            candidate->zones[i].latitude_e7 = get_i32_be(&value[offset]); offset += 4U;
            candidate->zones[i].longitude_e7 = get_i32_be(&value[offset]); offset += 4U;
            candidate->zones[i].radius_m = get_u16_be(&value[offset]); offset += 2U;
        }
        return 0;
    }
    if (id == MICRO_SETTING_BEACON_LIST || id == MICRO_SETTING_TRUSTED_DEVICE_LIST) {
        if (value_len % MICRO_DEVICE_ID_BYTES != 0U || value_len / MICRO_DEVICE_ID_BYTES > 4U) {
            return -EINVAL;
        }
        uint8_t count = (uint8_t)(value_len / MICRO_DEVICE_ID_BYTES);
        if (id == MICRO_SETTING_BEACON_LIST) {
            candidate->beacon_count = count;
            memset(candidate->beacons, 0, sizeof(candidate->beacons));
            memcpy(candidate->beacons, value, value_len);
        } else {
            candidate->trusted_device_count = count;
            memset(candidate->trusted_devices, 0, sizeof(candidate->trusted_devices));
            memcpy(candidate->trusted_devices, value, value_len);
        }
        return 0;
    }
    return -ENOENT;
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
        payload_len < 19U || !valid_imei(expected_imei)) {
        set_error(error_text, error_text_len, "invalid fixed settings payload");
        return -EINVAL;
    }
    if (memcmp(payload, expected_imei, 15U) != 0) {
        set_error(error_text, error_text_len, "target IMEI mismatch");
        return -EACCES;
    }
    if (payload[15] != MICRO_SETTINGS_SCHEMA_VERSION) {
        set_error(error_text, error_text_len, "unsupported schema version");
        return -EPROTONOSUPPORT;
    }
    uint8_t entry_count = payload[18];
    if (entry_count == 0U || entry_count > MAX_TLV_ENTRIES) {
        set_error(error_text, error_text_len, "invalid entry count");
        return -ERANGE;
    }
    memset(settings_scratch.seen, 0, sizeof(settings_scratch.seen));
    *candidate = *current;
    memset(result, 0, sizeof(*result));
    result->update_id = get_u16_be(&payload[16]);
    size_t offset = 19U;

    for (uint8_t i = 0U; i < entry_count; ++i) {
        if (offset + 4U > payload_len) {
            set_error(error_text, error_text_len, "truncated TLV header");
            return -EMSGSIZE;
        }
        uint8_t id = payload[offset++];
        uint8_t type = payload[offset++];
        uint16_t value_len = get_u16_be(&payload[offset]); offset += 2U;
        if (offset + value_len > payload_len) {
            set_error(error_text, error_text_len, "truncated TLV value");
            return -EMSGSIZE;
        }
        const struct micro_setting_definition *definition = micro_setting_by_id(id);
        if (definition == NULL) {
            set_error(error_text, error_text_len, "unknown setting ID");
            return -ENOENT;
        }
        if (settings_scratch.seen[id]) {
            set_error(error_text, error_text_len, "duplicate setting ID");
            return -EEXIST;
        }
        settings_scratch.seen[id] = true;
        if (type != definition->value_type) {
            set_error(error_text, error_text_len, "incorrect value type");
            return -EPROTOTYPE;
        }
        const uint8_t *value = &payload[offset];
        settings_scratch.before_entry = *candidate;
        int err = 0;
        if (type == MICRO_VALUE_UINT16) {
            if (value_len != 2U) {
                set_error(error_text, error_text_len, "incorrect uint16 length");
                return -EMSGSIZE;
            }
            uint16_t decoded = get_u16_be(value);
            if (decoded < definition->minimum || decoded > definition->maximum) {
                set_error(error_text, error_text_len, "numeric value out of range");
                return -ERANGE;
            }
            if (id == MICRO_SETTING_HEARTBEAT_INTERVAL) candidate->heartbeat_interval_seconds = decoded;
            else if (id == MICRO_SETTING_LTE_UPDATE_INTERVAL) candidate->lte_update_interval_seconds = decoded;
            else if (id == MICRO_SETTING_SLEEP_INTERVAL) candidate->sleep_interval_seconds = decoded;
            else err = -ENOENT;
        } else if (type == MICRO_VALUE_RAW) {
            err = apply_raw_setting(id, value, value_len, candidate);
        } else {
            err = -ENOTSUP;
        }
        if (err != 0) {
            set_error(error_text, error_text_len, "unsupported or invalid setting value");
            return err;
        }
        if (memcmp(&settings_scratch.before_entry, candidate,
                   sizeof(settings_scratch.before_entry)) != 0 &&
            result->changed_count < sizeof(result->changed_ids)) {
            result->changed_ids[result->changed_count++] = id;
        }
        offset += value_len;
    }
    if (offset != payload_len) {
        set_error(error_text, error_text_len, "unexpected trailing payload bytes");
        return -EMSGSIZE;
    }
    int err = micro_settings_validate_config(candidate);
    if (err != 0) {
        set_error(error_text, error_text_len, "candidate configuration failed validation");
        return err;
    }
    return 0;
}
