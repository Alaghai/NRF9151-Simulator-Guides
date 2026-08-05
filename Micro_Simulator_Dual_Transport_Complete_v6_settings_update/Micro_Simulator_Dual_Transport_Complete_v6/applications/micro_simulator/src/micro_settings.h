#ifndef MICRO_SETTINGS_H_
#define MICRO_SETTINGS_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define MICRO_SETTINGS_COMMAND 0x20U
#define MICRO_SETTINGS_SCHEMA_VERSION 0x01U
#define MICRO_MAX_SAFE_ZONES 4U
#define MICRO_MAX_BEACONS 4U
#define MICRO_MAX_TRUSTED_DEVICES 4U
#define MICRO_DEVICE_ID_BYTES 6U
#define MICRO_SETTINGS_MAGIC 0x4D534346UL /* MSCF */
#define MICRO_SETTINGS_STORAGE_VERSION 1U

#define MICRO_VALUE_UINT8 0x01U
#define MICRO_VALUE_UINT16 0x02U
#define MICRO_VALUE_UINT32 0x03U
#define MICRO_VALUE_INT32 0x04U
#define MICRO_VALUE_BOOL 0x05U
#define MICRO_VALUE_RAW 0x06U
#define MICRO_VALUE_UTF8 0x07U

#define MICRO_SETTING_HEARTBEAT_INTERVAL 0x01U
#define MICRO_SETTING_LTE_UPDATE_INTERVAL 0x02U
#define MICRO_SETTING_SLEEP_INTERVAL 0x03U
#define MICRO_SETTING_SAFE_ZONES 0x10U
#define MICRO_SETTING_BEACON_LIST 0x11U
#define MICRO_SETTING_TRUSTED_DEVICE_LIST 0x12U

struct micro_safe_zone {
    int32_t latitude_e7;
    int32_t longitude_e7;
    uint16_t radius_m;
};

struct micro_persistent_config {
    uint32_t magic;
    uint16_t storage_version;
    uint16_t heartbeat_interval_seconds;
    uint16_t lte_update_interval_seconds;
    uint16_t sleep_interval_seconds;
    uint8_t zone_count;
    struct micro_safe_zone zones[MICRO_MAX_SAFE_ZONES];
    uint8_t beacon_count;
    uint8_t beacons[MICRO_MAX_BEACONS][MICRO_DEVICE_ID_BYTES];
    uint8_t trusted_device_count;
    uint8_t trusted_devices[MICRO_MAX_TRUSTED_DEVICES][MICRO_DEVICE_ID_BYTES];
};

struct micro_setting_definition {
    uint8_t id;
    const char *name;
    uint8_t value_type;
    uint16_t fixed_length;
    uint32_t minimum;
    uint32_t maximum;
    uint32_t default_value;
    bool persistent;
};

struct micro_settings_apply_result {
    uint16_t update_id;
    uint8_t changed_count;
    uint8_t changed_ids[16];
};

void micro_settings_defaults(struct micro_persistent_config *config);
int micro_settings_validate_config(const struct micro_persistent_config *config);
const struct micro_setting_definition *micro_setting_by_name(const char *name);
const struct micro_setting_definition *micro_setting_by_id(uint8_t id);
size_t micro_setting_definition_count(void);
const struct micro_setting_definition *micro_setting_definition_at(size_t index);

int micro_settings_build_single_payload(const char *imei,
                                        uint16_t update_id,
                                        const char *name,
                                        const char *value_text,
                                        uint8_t *payload,
                                        size_t payload_max,
                                        size_t *payload_len);

int micro_settings_build_config_payload(const char *imei,
                                        uint16_t update_id,
                                        const struct micro_persistent_config *config,
                                        uint8_t *payload,
                                        size_t payload_max,
                                        size_t *payload_len);

int micro_settings_apply_payload(const uint8_t *payload,
                                 size_t payload_len,
                                 const char *expected_imei,
                                 const struct micro_persistent_config *current,
                                 struct micro_persistent_config *candidate,
                                 struct micro_settings_apply_result *result,
                                 char *error_text,
                                 size_t error_text_len);

#endif
