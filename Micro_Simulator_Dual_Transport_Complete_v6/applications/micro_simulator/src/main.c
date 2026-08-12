/*
 * Micro serial-controlled simulator for nRF9151 DK + Onomondo SoftSIM.
 *
 * Clean-slate application goals:
 * - Keep Onomondo external SoftSIM provisioning over serial.
 * - Work with a provisioned SoftSIM over LTE-M when LTE-M is available.
 * - Work without a provisioned SoftSIM by relaying through a Windows laptop.
 * - Let the user choose LTE TCP or serial/Wi-Fi relay transport.
 * - Keep the TCP socket open after connect.
 * - Send Micro heartbeat/location packets on command.
 * - Receive server responses in both LTE and relay modes.
 *
 * Protocol rules implemented by this revision:
 * - Every multi-byte integer in the Micro packet uses big-endian/network byte order.
 * - Length is a big-endian uint16 counting Command + Payload bytes.
 * - CRC is CRC-16/XMODEM (poly 0x1021, init 0x0000, xorout 0x0000).
 * - CRC covers Payload only (the bytes after Command).
 * - CRC is written big-endian in the packet.
 * - Sequence ID, timestamp, lastUpdate, coordinates, accuracy, and speed
 *   are all written big-endian.
 * - Timestamp is uint64 big-endian Unix time in milliseconds since
 *   1970-01-01T00:00:00Z (UTC). A value of zero means unavailable.
 */

#include <ctype.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/net/socket.h>
#include <zephyr/settings/settings.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/util.h>

#include <modem/lte_lc.h>
#include <modem/nrf_modem_lib.h>

#include <nrf_softsim.h>

#include "micro_settings.h"

LOG_MODULE_REGISTER(micro_simulator, LOG_LEVEL_INF);

#define DEFAULT_SERVER_IP "137.184.163.176"
#define DEFAULT_SERVER_PORT 5000
#define DEFAULT_IMEI "861352064050787"
#define DEFAULT_TIMESTAMP_UNIX_MS 1784640600000ULL /* 2026-07-21T13:30:00.000Z */
#define DEFAULT_TIMESTAMP_STEP_MS 5000ULL
#define DEFAULT_FIX_AGE_SECONDS 0U

/* Coordinates are signed int32 values in big-endian byte order.
 * Stored integer = decimal degrees x 1,000,000.
 */
#define MICRO_COORD_SCALE 1000000

#define PROFILE_MAX_SIZE 1024
#define PROFILE_IDLE_DONE_MS 2500
#define CLI_LINE_MAX 1024
#define MAX_PACKET_BYTES 512
#define MAX_HEX_CHARS ((MAX_PACKET_BYTES * 2) + 1)
#define SERVER_RESPONSE_MAX 1024
#define SETTINGS_ERROR_TEXT_MAX 96

#define TCP_RECV_POLL_MS 1000
#define RELAY_PREFIX_MAX 64
#define MICRO_HEARTBEAT_STACK_SIZE 4096
#define MICRO_STATE_MACHINE_STACK_SIZE 3072
#define MICRO_TCP_RX_STACK_SIZE 4096

K_SEM_DEFINE(lte_connected, 0, 1);

static const struct device *const uart_dev = DEVICE_DT_GET(DT_NODELABEL(uart0));

/* Corrected known-good packets generated from the default simulator state.
 * Sequence ID is 1 in each fixed sample. CRC covers Payload only and is
 * serialized big-endian.
 */
static const char SAMPLE_HB_BEACON[] =
    "AB10003E621D0001013836313335323036343035303738370000019F84DE77C0010107FF0101010FAC91003B9101AABBCCDDEE01000000000000000000000000000000000000";

static const char SAMPLE_HB_GPS[] =
    "AB10004464B10001013836313335323036343035303738370000019F84DE77C0010107FF01011002B513BCFB7CF3D000FF000001AABBCCDDEE01000000000000000000000000000000000000";

static const char SAMPLE_LOCATION[] =
    "AB10002867C80001103836313335323036343035303738370000019F84DE77C0010107FF02B513BCFB7CF3D000FF0000";

enum wire_mode {
    /* ASCII-HEX sends characters such as 'A' and 'B' (bytes 0x41, 0x42).
     * Binary sends the actual packet header byte 0xAB. Binary is the default
     * integration mode; ASCII-HEX is retained for readable diagnostics.
     */
    WIRE_ASCII_HEX = 0,
    WIRE_BINARY = 1,
};

enum transport_mode {
    TRANSPORT_LTE_TCP = 0,
    TRANSPORT_SERIAL_RELAY = 1,
};

enum heartbeat_opcode {
    OPCODE_BEACON = 0x01,
    OPCODE_GPS_SAFEZONE = 0x0A,
    OPCODE_GPS_LTE = 0x10,
    OPCODE_TRUSTED = 0xA0,
};

/* Configuration mode is deliberately quiet: it lets a tester inspect or
 * replace the persistent configuration without starting network activity.
 * Runtime simulation is an explicit opt-in mode. */
enum simulator_operation_mode {
    SIMULATOR_CONFIGURATION_MODE = 0,
    SIMULATOR_RUNTIME_MODE = 1,
};

enum device_tracking_state {
    DEVICE_STATE_BEACON = 0,
    DEVICE_STATE_TRUSTED_DEVICE,
    DEVICE_STATE_GPS_SAFE_ZONE,
    DEVICE_STATE_GPS_LTE_OUTSIDE,
};

enum timestamp_mode {
    TIMESTAMP_FIXED = 0,
    TIMESTAMP_RUNNING = 1,
    TIMESTAMP_STEP = 2,
};

struct sim_state {
    char server_ip[16];
    uint16_t server_port;
    int tcp_fd;
    bool tcp_connected;
    bool relay_connected;
    enum wire_mode mode;
    enum transport_mode transport;

    bool softsim_provisioned;
    bool modem_initialized;
    bool lte_connecting;
    bool lte_registered;

    uint16_t sequence_id;
    char imei[17];

    uint8_t battery;      /* 0x00 low, 0x01 medium, 0x10 high */
    uint8_t charging;     /* 0x01 not charging, 0x10 charging */
    uint16_t last_update; /* minutes */
    uint8_t sw_version;
    uint8_t fw_version;
    uint8_t opcode;
    enum simulator_operation_mode operation_mode;
    enum device_tracking_state tracking_state;

    enum timestamp_mode timestamp_mode;
    uint64_t timestamp_base_ms;
    int64_t timestamp_base_uptime_ms;
    uint64_t timestamp_step_ms;
    uint32_t location_fix_age_seconds;

    int32_t lat_e6;
    int32_t lon_e6;
    uint16_t accuracy_x10;
    uint16_t speed_x10;

    uint8_t beacon_mac[6];
    uint8_t trusted_addr[6]; /* currently detected trusted-device identity */
    bool beacon_detected;
    bool trusted_device_detected;
};

static struct sim_state state = {
    .server_ip = DEFAULT_SERVER_IP,
    .server_port = DEFAULT_SERVER_PORT,
    .tcp_fd = -1,
    .tcp_connected = false,
    .relay_connected = false,
    .mode = WIRE_BINARY,
    .transport = TRANSPORT_SERIAL_RELAY,
    .softsim_provisioned = false,
    .modem_initialized = false,
    .lte_connecting = false,
    .lte_registered = false,
    .sequence_id = 1,
    .imei = DEFAULT_IMEI,
    .battery = 0x01,
    .charging = 0x01,
    .last_update = 2047,
    .sw_version = 1,
    .fw_version = 1,
    .opcode = OPCODE_BEACON,
    .operation_mode = SIMULATOR_CONFIGURATION_MODE,
    .tracking_state = DEVICE_STATE_BEACON,
    .timestamp_mode = TIMESTAMP_FIXED,
    .timestamp_base_ms = DEFAULT_TIMESTAMP_UNIX_MS,
    .timestamp_base_uptime_ms = 0,
    .timestamp_step_ms = DEFAULT_TIMESTAMP_STEP_MS,
    .location_fix_age_seconds = DEFAULT_FIX_AGE_SECONDS,
    .lat_e6 = 45421500,
    .lon_e6 = -75697200,
    .accuracy_x10 = 255,
    .speed_x10 = 0,
    .beacon_mac = {0x0F, 0xAC, 0x91, 0x00, 0x3B, 0x91},
    .trusted_addr = {0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01},
    .beacon_detected = true,
    .trusted_device_detected = true,
};


static struct micro_persistent_config active_config;
static struct micro_settings_apply_result last_config_result;
static int last_config_status;
static char last_config_source[16] = "none";
static char last_config_error[SETTINGS_ERROR_TEXT_MAX];
static uint32_t last_config_uptime_ms;
static uint16_t next_update_id = 1U;
static bool config_loaded_from_storage;

static size_t server_response_buffer_len;
static uint8_t tcp_rx_chunk[SERVER_RESPONSE_MAX];

/* These buffers are shared by serialized TX/CLI/parser work. Keeping them out
 * of system-workqueue/main-thread frames avoids large stack use; TCP receive
 * uses its own tcp_rx_chunk and is the sole socket-reader. */
struct micro_transport_buffers {
    uint8_t packet[MAX_PACKET_BYTES];
    uint8_t payload[MAX_PACKET_BYTES];
    uint8_t binary[SERVER_RESPONSE_MAX];
    uint8_t response[SERVER_RESPONSE_MAX];
    char hex[MAX_HEX_CHARS];
    char token[96];
};

static struct micro_transport_buffers transport_buffers;

struct micro_settings_parse_context {
    char error_text[SETTINGS_ERROR_TEXT_MAX];
    struct micro_settings_apply_result result;
    struct micro_persistent_config candidate;
    struct micro_persistent_config previous;
};

static struct micro_settings_parse_context settings_parse_context;
static char softsim_profile[PROFILE_MAX_SIZE];
static char cli_line[CLI_LINE_MAX];

K_MUTEX_DEFINE(micro_transport_mutex);
K_SEM_DEFINE(micro_heartbeat_trigger, 0, 1);
K_SEM_DEFINE(micro_state_reevaluation_trigger, 0, 1);
K_SEM_DEFINE(micro_lte_location_trigger, 0, 1);
K_SEM_DEFINE(micro_tcp_rx_start, 0, 1);

static void heartbeat_timer_expiry(struct k_timer *timer);
K_TIMER_DEFINE(micro_heartbeat_timer, heartbeat_timer_expiry, NULL);
static void ble_check_timer_expiry(struct k_timer *timer);
K_TIMER_DEFINE(micro_ble_check_timer, ble_check_timer_expiry, NULL);
static void lte_location_timer_expiry(struct k_timer *timer);
K_TIMER_DEFINE(micro_lte_location_timer, lte_location_timer_expiry, NULL);

static bool heartbeat_timer_armed;
static bool ble_check_timer_armed;
static bool lte_location_timer_armed;

static void heartbeat_thread(void *p1, void *p2, void *p3);
K_THREAD_DEFINE(micro_heartbeat, MICRO_HEARTBEAT_STACK_SIZE, heartbeat_thread,
                NULL, NULL, NULL, 5, 0, 0);
static void state_machine_thread(void *p1, void *p2, void *p3);
K_THREAD_DEFINE(micro_state_machine, MICRO_STATE_MACHINE_STACK_SIZE, state_machine_thread,
                NULL, NULL, NULL, 5, 0, 0);
static void lte_location_thread(void *p1, void *p2, void *p3);
K_THREAD_DEFINE(micro_lte_location, MICRO_STATE_MACHINE_STACK_SIZE, lte_location_thread,
                NULL, NULL, NULL, 5, 0, 0);
static void tcp_rx_thread(void *p1, void *p2, void *p3);
K_THREAD_DEFINE(micro_tcp_rx, MICRO_TCP_RX_STACK_SIZE, tcp_rx_thread,
                NULL, NULL, NULL, 5, 0, 0);

static k_tid_t micro_main_thread;

/* Forward declarations. Keep these above provisioning code because C99 does not
 * allow implicit function declarations, and Zephyr builds with strict flags.
 */
static int hex_nibble(char c);
static int hex_to_bytes(const char *hex, uint8_t *out, size_t out_max, size_t *out_len);
static void bytes_to_hex(const uint8_t *bytes, size_t len, char *hex, size_t hex_len);
static void tcp_disconnect(void);
static int tcp_connect_to_server(const char *ip, uint16_t port);
static int tcp_recv_response(void);
static int tcp_send_hex_payload(const char *hex);
static void relay_disconnect(void);
static int relay_connect_to_server(const char *ip, uint16_t port);
static int relay_send_hex_payload(const char *hex);
static int transport_send_hex_payload(const char *hex);
static int modem_prepare(void);
static int lte_connect_start(void);
static int process_server_response_bytes(const uint8_t *data, size_t len, const char *source);
static void schedule_next_heartbeat(void);
static void schedule_ble_checks(void);
static void schedule_lte_location_updates(bool send_now);
static void request_state_reevaluation(void);
static int apply_settings_packet_bytes(const uint8_t *packet, size_t packet_len, const char *source);
static void print_hex6(const uint8_t v[6]);
static void print_persistent_config(void);


static void drain_logs_and_reboot(void)
{
    while (log_data_pending()) {
        log_process();
        k_yield();
    }

    sys_reboot(0);
}


/* -------------------------------------------------------------------------- */
/* Robust serial input                                                        */
/* -------------------------------------------------------------------------- */

/*
 * Use UART interrupt input instead of polling.
 * Polling can miss bytes when a full SoftSIM profile is pasted quickly from a
 * terminal. The previous working firmware used interrupt-driven input, so this
 * version keeps that behavior for both SoftSIM provisioning and the CLI.
 */
K_MSGQ_DEFINE(serial_char_msgq, sizeof(uint8_t), 2048, 4);

static void serial_uart_cb(const struct device *dev, void *user_data)
{
    ARG_UNUSED(user_data);

    if (!uart_irq_update(dev)) {
        return;
    }

    while (uart_irq_rx_ready(dev)) {
        uint8_t c;
        int rx = uart_fifo_read(dev, &c, 1);

        if (rx <= 0) {
            return;
        }

        (void)k_msgq_put(&serial_char_msgq, &c, K_NO_WAIT);
    }
}

static int serial_input_start(void)
{
    if (!device_is_ready(uart_dev)) {
        LOG_ERR("UART device not ready");
        return -ENODEV;
    }

    int err = uart_irq_callback_user_data_set(uart_dev, serial_uart_cb, NULL);
    if (err) {
        LOG_ERR("Failed to set UART callback: %d", err);
        return err;
    }

    uart_irq_rx_enable(uart_dev);
    return 0;
}

static int serial_get_char(uint8_t *c, k_timeout_t timeout)
{
    return k_msgq_get(&serial_char_msgq, c, timeout);
}

static int read_line_serial(char *buf, size_t buf_len, bool echo)
{
    size_t pos = 0;

    if (buf_len == 0) {
        return -EINVAL;
    }

    memset(buf, 0, buf_len);

    while (true) {
        uint8_t c;
        int err = serial_get_char(&c, K_FOREVER);
        if (err) {
            return err;
        }

        if (c == '\r' || c == '\n') {
            if (echo) {
                printk("\r\n");
            }
            buf[pos] = '\0';
            return (int)pos;
        }

        if (c == 0x08 || c == 0x7F) {
            if (pos > 0) {
                pos--;
                buf[pos] = '\0';
                if (echo) {
                    printk("\b \b");
                }
            }
            continue;
        }

        if (pos < buf_len - 1) {
            buf[pos++] = (char)c;
            if (echo) {
                uart_poll_out(uart_dev, c);
            }
        }
    }
}

static int read_softsim_profile(char *buf, size_t buf_len)
{
    size_t pos = 0;
    bool started = false;
    size_t visible_count = 0;

    if (buf_len == 0) {
        return -EINVAL;
    }

    memset(buf, 0, buf_len);

    while (true) {
        uint8_t c;
        k_timeout_t timeout = started ? K_MSEC(PROFILE_IDLE_DONE_MS) : K_FOREVER;
        int err = serial_get_char(&c, timeout);

        if (err == -EAGAIN) {
            if (started) {
                buf[pos] = '\0';
                printk("\r\n");
                return (int)pos;
            }
            continue;
        }

        if (err) {
            return err;
        }

        if (c == '\r' || c == '\n' || c == ' ' || c == '\t') {
            /* Ignore whitespace so profiles copied with wrapping still work. */
            continue;
        }

        if (hex_nibble((char)c) < 0) {
            printk("\r\nInvalid non-hex character received: 0x%02X\r\n", c);
            return -EINVAL;
        }

        started = true;

        if (pos >= buf_len - 1) {
            printk("\r\nSoftSIM profile too long for buffer\r\n");
            return -ENOMEM;
        }

        buf[pos++] = (char)c;

        /* Progress indicator without printing the secret profile. */
        visible_count++;
        if ((visible_count % 64) == 0) {
            printk(".");
        }
    }
}

static int provision_softsim_from_serial(void)
{
    if (!device_is_ready(uart_dev)) {
        LOG_ERR("UART device not ready");
        return -ENODEV;
    }

    LOG_INF("Transfer SoftSIM profile using serial COM port.");
    LOG_INF("Paste the complete HEX profile. Whitespace and line breaks are ignored.");
    LOG_INF("After paste, wait about 3 seconds; the firmware will provision automatically.");

    int len = read_softsim_profile(softsim_profile, sizeof(softsim_profile));
    if (len <= 0) {
        LOG_ERR("Invalid SoftSIM profile length: %d", len);
        return -EINVAL;
    }

    LOG_INF("Profile received: %d hex characters in total", len);

    if ((len % 2) != 0) {
        LOG_ERR("SoftSIM profile has odd hex character count; profile is incomplete");
        return -EINVAL;
    }

    int err = nrf_softsim_provision((uint8_t *)softsim_profile, (size_t)len);
    if (err != 0) {
        LOG_ERR("SoftSIM profile provisioning failed: %d", err);
        LOG_ERR("Most common cause: only part of the profile was pasted or the profile contains extra text.");
        return err;
    }

    LOG_INF("SoftSIM provisioned; rebooting");
    drain_logs_and_reboot();

    return 0;
}

static void lte_handler(const struct lte_lc_evt *const evt)
{
    switch (evt->type) {
    case LTE_LC_EVT_NW_REG_STATUS:
        if ((evt->nw_reg_status == LTE_LC_NW_REG_REGISTERED_HOME) ||
            (evt->nw_reg_status == LTE_LC_NW_REG_REGISTERED_ROAMING)) {
            state.lte_registered = true;
            state.lte_connecting = false;
            LOG_INF("Network registration status: %s",
                evt->nw_reg_status == LTE_LC_NW_REG_REGISTERED_HOME ?
                "Connected - home network" : "Connected - roaming");
            k_sem_give(&lte_connected);
        } else {
            state.lte_registered = false;
        }
        break;

    case LTE_LC_EVT_RRC_UPDATE:
        LOG_INF("RRC mode: %s",
            evt->rrc_mode == LTE_LC_RRC_MODE_CONNECTED ? "Connected" : "Idle");
        break;

    case LTE_LC_EVT_CELL_UPDATE:
        LOG_INF("LTE cell changed: Cell ID: %d, Tracking area: %d",
            evt->cell.id, evt->cell.tac);
        break;

    default:
        break;
    }
}

static int modem_prepare(void)
{
    if (!state.softsim_provisioned) {
        printk("SoftSIM is not provisioned. Use relay mode or run: softsim provision\r\n");
        return -ENODEV;
    }

    if (state.modem_initialized) {
        return 0;
    }

    int err = nrf_modem_lib_init();
    if (err) {
        LOG_ERR("Failed to initialize modem library: %d", err);
        return err;
    }

    state.modem_initialized = true;
    return 0;
}

static int lte_connect_start(void)
{
    if (state.lte_registered) {
        printk("LTE is already connected\r\n");
        return 0;
    }

    if (state.lte_connecting) {
        printk("LTE connection is already in progress\r\n");
        return 0;
    }

    int err = modem_prepare();
    if (err) {
        return err;
    }

    err = lte_lc_connect_async(lte_handler);
    if (err) {
        LOG_ERR("Connecting to LTE network failed: %d", err);
        return err;
    }

    state.lte_connecting = true;
    LOG_INF("LTE connection started in background");
    return 0;
}

static int hex_nibble(char c)
{
    if (c >= '0' && c <= '9') {
        return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }
    return -1;
}

static int hex_to_bytes(const char *hex, uint8_t *out, size_t out_max, size_t *out_len)
{
    int high = -1;
    size_t len = 0;

    for (size_t i = 0; hex[i] != '\0'; i++) {
        if (isspace((unsigned char)hex[i])) {
            continue;
        }

        int n = hex_nibble(hex[i]);
        if (n < 0) {
            return -EINVAL;
        }

        if (high < 0) {
            high = n;
        } else {
            if (len >= out_max) {
                return -ENOMEM;
            }
            out[len++] = (uint8_t)((high << 4) | n);
            high = -1;
        }
    }

    if (high >= 0) {
        return -EINVAL;
    }

    *out_len = len;
    return 0;
}

static void bytes_to_hex(const uint8_t *bytes, size_t len, char *hex, size_t hex_len)
{
    static const char digits[] = "0123456789ABCDEF";

    if (hex_len < (len * 2) + 1) {
        if (hex_len > 0) {
            hex[0] = '\0';
        }
        return;
    }

    for (size_t i = 0; i < len; i++) {
        hex[i * 2] = digits[(bytes[i] >> 4) & 0x0F];
        hex[(i * 2) + 1] = digits[bytes[i] & 0x0F];
    }
    hex[len * 2] = '\0';
}

static void append_u8(uint8_t *buf, size_t *len, uint8_t v)
{
    buf[(*len)++] = v;
}

static void append_u16_be(uint8_t *buf, size_t *len, uint16_t v)
{
    buf[(*len)++] = (uint8_t)((v >> 8) & 0xFF);
    buf[(*len)++] = (uint8_t)(v & 0xFF);
}

static void append_u64_be(uint8_t *buf, size_t *len, uint64_t v)
{
    buf[(*len)++] = (uint8_t)((v >> 56) & 0xFF);
    buf[(*len)++] = (uint8_t)((v >> 48) & 0xFF);
    buf[(*len)++] = (uint8_t)((v >> 40) & 0xFF);
    buf[(*len)++] = (uint8_t)((v >> 32) & 0xFF);
    buf[(*len)++] = (uint8_t)((v >> 24) & 0xFF);
    buf[(*len)++] = (uint8_t)((v >> 16) & 0xFF);
    buf[(*len)++] = (uint8_t)((v >> 8) & 0xFF);
    buf[(*len)++] = (uint8_t)(v & 0xFF);
}

static void append_i32_be(uint8_t *buf, size_t *len, int32_t v)
{
    uint32_t u = (uint32_t)v;
    buf[(*len)++] = (uint8_t)((u >> 24) & 0xFF);
    buf[(*len)++] = (uint8_t)((u >> 16) & 0xFF);
    buf[(*len)++] = (uint8_t)((u >> 8) & 0xFF);
    buf[(*len)++] = (uint8_t)(u & 0xFF);
}

static void append_bytes(uint8_t *buf, size_t *len, const uint8_t *src, size_t src_len)
{
    memcpy(&buf[*len], src, src_len);
    *len += src_len;
}

/* Exact C equivalent of the CRC routine supplied by the server team.
 * This is CRC-16/XMODEM: poly=0x1021, init=0x0000, refin=false,
 * refout=false, xorout=0x0000. The check value for ASCII "123456789"
 * is 0x31C3.
 */
static uint16_t calculate_crc16(const uint8_t *data, size_t data_len, uint16_t initial_crc)
{
    uint16_t crc = initial_crc;

    for (size_t i = 0; i < data_len; i++) {
        crc = (uint16_t)(((uint8_t)(crc >> 8)) | (uint16_t)(crc << 8));
        crc ^= data[i];
        crc ^= (uint16_t)(((uint8_t)(crc & 0xFF)) >> 4);
        crc ^= (uint16_t)((crc << 8) << 4);
        crc ^= (uint16_t)((((crc & 0xFF) << 4)) << 1);
    }

    return crc;
}


static int persistent_settings_set(const char *name, size_t len,
                                   settings_read_cb read_cb, void *cb_arg)
{
    if (strcmp(name, "config") != 0) {
        return -ENOENT;
    }
    if (len != sizeof(active_config)) {
        return -EMSGSIZE;
    }
    ssize_t read_len = read_cb(cb_arg, &active_config, sizeof(active_config));
    if (read_len != sizeof(active_config)) {
        return read_len < 0 ? (int)read_len : -EIO;
    }
    int err = micro_settings_validate_config(&active_config);
    if (err == 0) {
        config_loaded_from_storage = true;
    }
    return err;
}

SETTINGS_STATIC_HANDLER_DEFINE(micro_config, "micro", NULL,
                               persistent_settings_set, NULL, NULL);

static int save_active_config(const struct micro_persistent_config *candidate)
{
    if (micro_settings_validate_config(candidate) != 0) {
        return -EINVAL;
    }
    if (memcmp(candidate, &active_config, sizeof(*candidate)) == 0) {
        return 0;
    }
    int err = settings_save_one("micro/config", candidate, sizeof(*candidate));
    if (err == 0) {
        active_config = *candidate;
    }
    return err;
}

static int persistent_settings_init(void)
{
    micro_settings_defaults(&active_config);
    config_loaded_from_storage = false;
    int err = settings_subsys_init();
    if (err != 0 && err != -EALREADY) {
        LOG_ERR("settings subsystem init failed: %d", err);
        return err;
    }
    err = settings_load_subtree("micro");
    if (err != 0 || micro_settings_validate_config(&active_config) != 0) {
        LOG_WRN("No valid stored simulator configuration; using defaults");
        micro_settings_defaults(&active_config);
        err = settings_save_one("micro/config", &active_config, sizeof(active_config));
        if (err != 0) {
            LOG_ERR("Could not store default simulator configuration: %d", err);
            return err;
        }
    } else if (config_loaded_from_storage) {
        LOG_INF("Simulator configuration loaded from persistent storage (update ID %u)",
                active_config.last_update_id);
    }
    next_update_id = (uint16_t)(active_config.last_update_id + 1U);
    if (next_update_id == 0U) {
        next_update_id = 1U;
    }
    return 0;
}

static void record_config_result(int status, const char *source,
                                 const struct micro_settings_apply_result *result,
                                 const char *error_text)
{
    last_config_status = status;
    last_config_uptime_ms = (uint32_t)k_uptime_get_32();
    snprintf(last_config_source, sizeof(last_config_source), "%s", source ? source : "unknown");
    snprintf(last_config_error, sizeof(last_config_error), "%s", error_text ? error_text : "");
    if (result != NULL) {
        last_config_result = *result;
    } else {
        memset(&last_config_result, 0, sizeof(last_config_result));
    }
}

static void append_imei(uint8_t *buf, size_t *len)
{
    /* Canonical Version 7 uses exactly 15 ASCII decimal digits. */
    size_t imei_len = strlen(state.imei);
    if (imei_len > 15) {
        imei_len = 15;
    }
    append_bytes(buf, len, (const uint8_t *)state.imei, imei_len);
}

static int32_t coord_for_protocol(int32_t e6)
{
#if MICRO_COORD_SCALE == 1000000
    return e6;
#elif MICRO_COORD_SCALE == 100000
    return e6 / 10;
#else
#error "Unsupported MICRO_COORD_SCALE"
#endif
}

static void append_location_fields(uint8_t *buf, size_t *len)
{
    append_i32_be(buf, len, coord_for_protocol(state.lat_e6));
    append_i32_be(buf, len, coord_for_protocol(state.lon_e6));
    append_u16_be(buf, len, state.accuracy_x10);
    append_u16_be(buf, len, state.speed_x10);
}

static const char *timestamp_mode_name(void)
{
    switch (state.timestamp_mode) {
    case TIMESTAMP_FIXED: return "fixed";
    case TIMESTAMP_RUNNING: return "running";
    case TIMESTAMP_STEP: return "step";
    default: return "unknown";
    }
}

static uint64_t simulated_current_time_ms(void)
{
    if (state.timestamp_mode != TIMESTAMP_RUNNING) {
        return state.timestamp_base_ms;
    }

    int64_t elapsed_ms = k_uptime_get() - state.timestamp_base_uptime_ms;
    if (elapsed_ms <= 0) {
        return state.timestamp_base_ms;
    }

    uint64_t elapsed = (uint64_t)elapsed_ms;
    if (UINT64_MAX - state.timestamp_base_ms < elapsed) {
        return UINT64_MAX;
    }
    return state.timestamp_base_ms + elapsed;
}

static uint64_t timestamp_for_packet(bool location_packet)
{
    uint64_t timestamp_ms = simulated_current_time_ms();

    if (location_packet) {
        uint64_t fix_age_ms = (uint64_t)state.location_fix_age_seconds * 1000ULL;
        timestamp_ms = timestamp_ms >= fix_age_ms ? timestamp_ms - fix_age_ms : 0;
    }

    return timestamp_ms;
}

static void advance_step_timestamp_after_packet(void)
{
    if (state.timestamp_mode != TIMESTAMP_STEP) {
        return;
    }

    if (UINT64_MAX - state.timestamp_base_ms < state.timestamp_step_ms) {
        state.timestamp_base_ms = UINT64_MAX;
    } else {
        state.timestamp_base_ms += state.timestamp_step_ms;
    }
}

static void reset_timestamp_uptime_anchor(void)
{
    state.timestamp_base_uptime_ms = k_uptime_get();
}

#define PACKET_COMMAND_OFFSET 8U
#define PACKET_PAYLOAD_OFFSET 9U

static void start_packet(uint8_t *buf, size_t *len, uint8_t command)
{
    *len = 0;
    append_u8(buf, len, 0xAB);          /* Header */
    append_u8(buf, len, 0x10);          /* Property */
    append_u8(buf, len, 0x00);          /* Length placeholder, high */
    append_u8(buf, len, 0x00);          /* Length placeholder, low */
    append_u8(buf, len, 0x00);          /* CRC placeholder, high */
    append_u8(buf, len, 0x00);          /* CRC placeholder, low */
    append_u16_be(buf, len, state.sequence_id);
    append_u8(buf, len, command);
}

static int finalize_packet(uint8_t *buf, size_t len)
{
    if (len < PACKET_PAYLOAD_OFFSET || len > UINT16_MAX + 8U) {
        return -EINVAL;
    }

    /* Length counts Command + Payload, so it is total packet length minus
     * the eight bytes before Command.
     */
    uint16_t body_len = (uint16_t)(len - PACKET_COMMAND_OFFSET);
    buf[2] = (uint8_t)((body_len >> 8) & 0xFF);
    buf[3] = (uint8_t)(body_len & 0xFF);

    /* The server team specified CRC over Payload only. Command is not part
     * of the CRC input. Store the resulting uint16 big-endian.
     */
    uint16_t crc = calculate_crc16(&buf[PACKET_PAYLOAD_OFFSET],
                                   len - PACKET_PAYLOAD_OFFSET,
                                   0x0000);
    buf[4] = (uint8_t)((crc >> 8) & 0xFF);
    buf[5] = (uint8_t)(crc & 0xFF);

    return 0;
}

static int build_heartbeat_hex(char *hex, size_t hex_len)
{
    uint8_t *pkt = transport_buffers.packet;
    size_t len;

    start_packet(pkt, &len, 0x01);
    append_imei(pkt, &len);
    append_u64_be(pkt, &len, timestamp_for_packet(false));
    append_u8(pkt, &len, state.battery);
    append_u8(pkt, &len, state.charging);
    append_u16_be(pkt, &len, state.last_update);
    append_u8(pkt, &len, state.sw_version);
    append_u8(pkt, &len, state.fw_version);
    append_u8(pkt, &len, state.opcode);

    if (state.opcode == OPCODE_BEACON) {
        append_bytes(pkt, &len, state.beacon_mac, sizeof(state.beacon_mac));
    } else if (state.opcode == OPCODE_TRUSTED) {
        append_bytes(pkt, &len, state.trusted_addr, sizeof(state.trusted_addr));
    } else if (state.opcode == OPCODE_GPS_SAFEZONE || state.opcode == OPCODE_GPS_LTE) {
        append_location_fields(pkt, &len);
    } else {
        return -EINVAL;
    }

    /* Version 7 trusted-device extension. This is the configured registry,
     * independent of the opcode-selected device currently detected nearby.
     */
    append_u8(pkt, &len, active_config.trusted_device_count);
    for (uint8_t i = 0U; i < MICRO_MAX_TRUSTED_DEVICES; ++i) {
        append_bytes(pkt, &len, active_config.trusted_devices[i], MICRO_DEVICE_ID_BYTES);
    }

    int err = finalize_packet(pkt, len);
    if (err) {
        return err;
    }

    bytes_to_hex(pkt, len, hex, hex_len);
    state.sequence_id++;
    advance_step_timestamp_after_packet();

    return 0;
}

static int build_location_hex(char *hex, size_t hex_len)
{
    uint8_t *pkt = transport_buffers.packet;
    size_t len;

    start_packet(pkt, &len, 0x10);
    append_imei(pkt, &len);
    append_u64_be(pkt, &len, timestamp_for_packet(true));
    append_u8(pkt, &len, state.battery);
    append_u8(pkt, &len, state.charging);
    append_u16_be(pkt, &len, state.last_update);
    append_location_fields(pkt, &len);

    int err = finalize_packet(pkt, len);
    if (err) {
        return err;
    }

    bytes_to_hex(pkt, len, hex, hex_len);
    state.sequence_id++;
    advance_step_timestamp_after_packet();

    return 0;
}


static uint16_t read_u16_be(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

static int build_settings_update_hex(const char *name, const char *value,
                                     char *hex, size_t hex_len,
                                     uint16_t *generated_update_id)
{
    uint8_t *payload = transport_buffers.payload;
    size_t payload_len = 0U;
    uint16_t update_id = next_update_id++;
    int err = micro_settings_build_config_with_override_payload(state.imei, update_id,
                                                  &active_config, name, value,
                                                  payload, MAX_PACKET_BYTES,
                                                  &payload_len);
    if (err != 0) {
        return err;
    }
    uint8_t *packet = transport_buffers.packet;
    size_t packet_len = 0U;
    start_packet(packet, &packet_len, MICRO_SETTINGS_COMMAND);
    if (packet_len + payload_len > MAX_PACKET_BYTES) {
        return -ENOSPC;
    }
    append_bytes(packet, &packet_len, payload, payload_len);
    err = finalize_packet(packet, packet_len);
    if (err != 0) {
        return err;
    }
    bytes_to_hex(packet, packet_len, hex, hex_len);
    if (generated_update_id != NULL) {
        *generated_update_id = update_id;
    }
    state.sequence_id++;
    return 0;
}

static int build_settings_config_update_hex(const struct micro_persistent_config *config,
                                            char *hex, size_t hex_len,
                                            uint16_t *generated_update_id)
{
    uint8_t *payload = transport_buffers.payload;
    size_t payload_len = 0U;
    uint16_t update_id = next_update_id++;
    int err = micro_settings_build_config_payload(state.imei, update_id,
                                                   config,
                                                   payload, MAX_PACKET_BYTES,
                                                   &payload_len);
    if (err != 0) {
        return err;
    }
    uint8_t *packet = transport_buffers.packet;
    size_t packet_len = 0U;
    start_packet(packet, &packet_len, MICRO_SETTINGS_COMMAND);
    if (packet_len + payload_len > MAX_PACKET_BYTES) {
        return -ENOSPC;
    }
    append_bytes(packet, &packet_len, payload, payload_len);
    err = finalize_packet(packet, packet_len);
    if (err != 0) {
        return err;
    }
    bytes_to_hex(packet, packet_len, hex, hex_len);
    if (generated_update_id != NULL) {
        *generated_update_id = update_id;
    }
    state.sequence_id++;
    return 0;
}

static void print_settings_changes(const struct micro_persistent_config *previous,
                                   const struct micro_persistent_config *current,
                                   const struct micro_settings_apply_result *result)
{
    for (uint8_t index = 0U; index < result->changed_count; ++index) {
        uint8_t id = result->changed_ids[index];
        const struct micro_setting_definition *definition = micro_setting_by_id(id);
        const char *name = definition != NULL ? definition->name : "unknown";
        if (id == MICRO_SETTING_HEARTBEAT_INTERVAL) {
            printk("setting %s: %u -> %u\r\n", name,
                   previous->heartbeat_interval_seconds,
                   current->heartbeat_interval_seconds);
        } else if (id == MICRO_SETTING_LTE_UPDATE_INTERVAL) {
            printk("setting %s: %u -> %u\r\n", name,
                   previous->lte_update_interval_seconds,
                   current->lte_update_interval_seconds);
        } else if (id == MICRO_SETTING_BLE_CHECK_INTERVAL) {
            printk("setting %s: %u -> %u\r\n", name,
                   previous->ble_check_interval_seconds,
                   current->ble_check_interval_seconds);
        } else if (id == MICRO_SETTING_SAFE_ZONES) {
            printk("setting %s count: %u -> %u\r\n", name,
                   previous->zone_count, current->zone_count);
            for (uint8_t i = 0U; i < previous->zone_count; ++i) {
                printk("  old[%u]: lat_e6=%d lon_e6=%d radius_m=%u\r\n", i + 1U,
                       previous->zones[i].latitude_e6,
                       previous->zones[i].longitude_e6,
                       previous->zones[i].radius_m);
            }
            for (uint8_t i = 0U; i < current->zone_count; ++i) {
                printk("  new[%u]: lat_e6=%d lon_e6=%d radius_m=%u\r\n", i + 1U,
                       current->zones[i].latitude_e6,
                       current->zones[i].longitude_e6,
                       current->zones[i].radius_m);
            }
        } else if (id == MICRO_SETTING_BEACON_LIST) {
            printk("setting %s count: %u -> %u\r\n", name,
                   previous->beacon_count, current->beacon_count);
            for (uint8_t i = 0U; i < previous->beacon_count; ++i) {
                printk("  old[%u]: ", i + 1U); print_hex6(previous->beacons[i]); printk("\r\n");
            }
            for (uint8_t i = 0U; i < current->beacon_count; ++i) {
                printk("  new[%u]: ", i + 1U); print_hex6(current->beacons[i]); printk("\r\n");
            }
        } else if (id == MICRO_SETTING_TRUSTED_DEVICE_LIST) {
            printk("setting %s count: %u -> %u\r\n", name,
                   previous->trusted_device_count, current->trusted_device_count);
            for (uint8_t i = 0U; i < previous->trusted_device_count; ++i) {
                printk("  old[%u]: ", i + 1U); print_hex6(previous->trusted_devices[i]); printk("\r\n");
            }
            for (uint8_t i = 0U; i < current->trusted_device_count; ++i) {
                printk("  new[%u]: ", i + 1U); print_hex6(current->trusted_devices[i]); printk("\r\n");
            }
        } else if (id == MICRO_SETTING_SENDING_UPDATE) {
            printk("setting %s: 0x%02X -> 0x%02X\r\n", name,
                   previous->sending_update, current->sending_update);
        }
    }
}

static int apply_settings_packet_bytes(const uint8_t *packet, size_t packet_len,
                                       const char *source)
{
    struct micro_settings_parse_context *ctx = &settings_parse_context;
    memset(ctx, 0, sizeof(*ctx));

    if (packet == NULL || packet_len < PACKET_PAYLOAD_OFFSET) {
        record_config_result(-EMSGSIZE, source, NULL, "packet shorter than fixed envelope");
        return -EMSGSIZE;
    }
    if (packet[0] != 0xABU || packet[1] != 0x10U) {
        record_config_result(-EINVAL, source, NULL, "invalid header or property");
        return -EINVAL;
    }
    uint16_t declared = read_u16_be(&packet[2]);
    if ((size_t)declared + PACKET_COMMAND_OFFSET != packet_len) {
        record_config_result(-EMSGSIZE, source, NULL, "length mismatch");
        return -EMSGSIZE;
    }
    if (packet[PACKET_COMMAND_OFFSET] != MICRO_SETTINGS_COMMAND) {
        record_config_result(-EPROTO, source, NULL, "not a settings-update command");
        return -EPROTO;
    }
    uint16_t received_crc = read_u16_be(&packet[4]);
    uint16_t calculated_crc = calculate_crc16(&packet[PACKET_PAYLOAD_OFFSET],
                                              packet_len - PACKET_PAYLOAD_OFFSET,
                                              0x0000);
    if (received_crc != calculated_crc) {
        record_config_result(-EBADMSG, source, NULL, "CRC mismatch");
        return -EBADMSG;
    }

    int err = micro_settings_apply_payload(&packet[PACKET_PAYLOAD_OFFSET],
                                           packet_len - PACKET_PAYLOAD_OFFSET,
                                           state.imei,
                                           &active_config,
                                           &ctx->candidate,
                                           &ctx->result,
                                           ctx->error_text,
                                           sizeof(ctx->error_text));
    if (err != 0) {
        record_config_result(err, source, &ctx->result, ctx->error_text);
        printk("Settings rejected from %s: %s (%d)\r\n",
               source, ctx->error_text, err);
        return err;
    }

    printk("Settings update %u validated from %s; persisting\r\n",
           ctx->result.update_id, source);

    ctx->previous = active_config;
    err = save_active_config(&ctx->candidate);
    if (err != 0) {
        record_config_result(err, source, &ctx->result, "persistent storage write failed");
        printk("Settings validation succeeded but persistence failed: %d\r\n", err);
        return err;
    }

    record_config_result(0, source, &ctx->result, "");
    printk("Settings update %u persisted and applied from %s (%u changed setting(s))\r\n",
           ctx->result.update_id, source, ctx->result.changed_count);
    print_settings_changes(&ctx->previous, &active_config, &ctx->result);
    if (ctx->previous.heartbeat_interval_seconds != active_config.heartbeat_interval_seconds) {
        schedule_next_heartbeat();
    }
    if (ctx->previous.ble_check_interval_seconds != active_config.ble_check_interval_seconds) {
        schedule_ble_checks();
    }
    if (ctx->previous.lte_update_interval_seconds != active_config.lte_update_interval_seconds &&
        state.tracking_state == DEVICE_STATE_GPS_LTE_OUTSIDE) {
        schedule_lte_location_updates(false);
    }
    /* A full-replacement update can change the configured BLE/GNSS context.
     * Evaluate it immediately; this is what makes a lost-person update take
     * effect without waiting for the next BLE interval. */
    request_state_reevaluation();
    print_persistent_config();
    return 0;
}

static void tcp_disconnect(void)
{
    if (state.tcp_fd >= 0) {
        zsock_close(state.tcp_fd);
    }
    state.tcp_fd = -1;
    state.tcp_connected = false;
    (void)k_timer_stop(&micro_heartbeat_timer);
    heartbeat_timer_armed = false;
    printk("TCP disconnected\r\n");
}

static int tcp_connect_to_server(const char *ip, uint16_t port)
{
    struct sockaddr_in server4;

    if (state.tcp_connected) {
        tcp_disconnect();
    }

    memset(&server4, 0, sizeof(server4));
    server4.sin_family = AF_INET;
    server4.sin_port = htons(port);

    if (zsock_inet_pton(AF_INET, ip, &server4.sin_addr) != 1) {
        printk("Invalid IPv4 address: %s\r\n", ip);
        return -EINVAL;
    }

    int fd = zsock_socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd < 0) {
        printk("TCP socket create failed: errno=%d\r\n", errno);
        return -errno;
    }

    printk("Connecting to %s:%u ...\r\n", ip, port);

    if (zsock_connect(fd, (struct sockaddr *)&server4, sizeof(server4)) < 0) {
        int err = -errno;
        printk("TCP connect failed: errno=%d\r\n", errno);
        zsock_close(fd);
        return err;
    }

    state.tcp_fd = fd;
    state.tcp_connected = true;
    strncpy(state.server_ip, ip, sizeof(state.server_ip) - 1);
    state.server_ip[sizeof(state.server_ip) - 1] = '\0';
    state.server_port = port;

    printk("TCP connected to %s:%u\r\n", state.server_ip, state.server_port);
    server_response_buffer_len = 0U;
    /* The RX worker is the sole zsock_recv() owner for this connection. */
    k_sem_give(&micro_tcp_rx_start);
    schedule_next_heartbeat();
    return 0;
}

static int process_server_response_bytes(const uint8_t *data, size_t len,
                                         const char *source)
{
    if (len > sizeof(transport_buffers.response) - server_response_buffer_len) {
        printk("Server response buffer overflow; discarding buffered data\r\n");
        server_response_buffer_len = 0U;
        return -ENOSPC;
    }
    memcpy(&transport_buffers.response[server_response_buffer_len], data, len);
    server_response_buffer_len += len;
    int events = 0;

    while (server_response_buffer_len > 0U) {
        /* Canonical Version 8 settings delivery is a self-identifying binary
         * Micro packet.  A pending update starts with AB 10 and carries
         * command 0x02 at PACKET_COMMAND_OFFSET; no SUP pre-token is needed. */
        if (transport_buffers.response[0] == 0xABU) {
            if (server_response_buffer_len < 2U) {
                break;
            }
            if (transport_buffers.response[1] != 0x10U) {
                printk("Server binary packet has invalid property 0x%02X\r\n",
                       transport_buffers.response[1]);
                memmove(transport_buffers.response, &transport_buffers.response[1],
                        server_response_buffer_len - 1U);
                server_response_buffer_len--;
                events++;
                continue;
            }
            if (server_response_buffer_len < 4U) {
                break;
            }

            uint16_t declared = read_u16_be(&transport_buffers.response[2]);
            size_t total = PACKET_COMMAND_OFFSET + declared;
            if (declared < 1U || total > MAX_PACKET_BYTES) {
                printk("Incoming binary packet has invalid length %u\r\n", declared);
                memmove(transport_buffers.response, &transport_buffers.response[1],
                        server_response_buffer_len - 1U);
                server_response_buffer_len--;
                events++;
                continue;
            }
            if (server_response_buffer_len < total) {
                break;
            }

            uint8_t command = transport_buffers.response[PACKET_COMMAND_OFFSET];
            if (command == MICRO_SETTINGS_COMMAND) {
                printk("Complete command-0x02 configuration packet received from %s (%u bytes)\r\n",
                       source, (unsigned int)total);
                int err = apply_settings_packet_bytes(transport_buffers.response, total, source);
                if (err != 0) {
                    printk("Settings packet rejected: %d\r\n", err);
                }
            } else {
                printk("Unexpected binary server command 0x%02X discarded\r\n", command);
            }

            memmove(transport_buffers.response, &transport_buffers.response[total],
                    server_response_buffer_len - total);
            server_response_buffer_len -= total;
            events++;
            continue;
        }

        size_t newline = 0U;
        bool found = false;
        for (; newline < server_response_buffer_len; ++newline) {
            if (transport_buffers.response[newline] == '\n') {
                found = true;
                break;
            }
        }
        if (!found) {
            break;
        }

        size_t token_len = newline;
        while (token_len > 0U &&
               (transport_buffers.response[token_len - 1U] == '\r' ||
                transport_buffers.response[token_len - 1U] == ' ')) {
            token_len--;
        }
        size_t copy_len = MIN(token_len, sizeof(transport_buffers.token) - 1U);
        memcpy(transport_buffers.token, transport_buffers.response, copy_len);
        transport_buffers.token[copy_len] = '\0';
        memmove(transport_buffers.response,
                &transport_buffers.response[newline + 1U],
                server_response_buffer_len - newline - 1U);
        server_response_buffer_len -= newline + 1U;
        events++;

        if (strcmp(transport_buffers.token, "OK") == 0) {
            printk("OK received: heartbeat accepted; no configuration update pending\r\n");
        } else if (strncmp(transport_buffers.token, "ERROR", 5U) == 0) {
            printk("ERROR received from server: %s\r\n", transport_buffers.token);
        } else if (strcmp(transport_buffers.token, "SUP") == 0) {
            /* Backward-compatible diagnostic only.  Canonical Version 8 does
             * not require SUP; a following AB... command-0x02 packet will be
             * parsed normally by the next loop iteration. */
            printk("Legacy SUP token received; canonical V8 expects binary command-0x02 directly\r\n");
        } else if (strcmp(transport_buffers.token, "FWUP") == 0) {
            printk("FWUP received: firmware transfer handling is deferred and unsupported\r\n");
        } else if (strcmp(transport_buffers.token, "CONFIG_NONE") == 0) {
            printk("CONFIG_NONE received (legacy diagnostic response)\r\n");
        } else {
            printk("Unknown server response preserved: %s\r\n", transport_buffers.token);
        }
    }
    return events;
}

static int tcp_recv_response(void)
{
    printk("TCP RX is handled continuously by the dedicated receiver\r\n");
    return 0;
}

static void tcp_rx_thread(void *p1, void *p2, void *p3)
{
    ARG_UNUSED(p1);
    ARG_UNUSED(p2);
    ARG_UNUSED(p3);
    (void)k_thread_name_set(k_current_get(), "micro_tcp_rx");

    while (true) {
        (void)k_sem_take(&micro_tcp_rx_start, K_FOREVER);
        while (state.tcp_connected && state.tcp_fd >= 0) {
            struct zsock_pollfd pfd = {
                .fd = state.tcp_fd,
                .events = ZSOCK_POLLIN,
                .revents = 0,
            };
            int poll_result = zsock_poll(&pfd, 1, TCP_RECV_POLL_MS);
            if (poll_result == 0) {
                continue;
            }
            if (poll_result < 0) {
                printk("TCP RX poll failed: errno=%d\r\n", errno);
                break;
            }
            if ((pfd.revents & ZSOCK_POLLIN) == 0) {
                printk("TCP RX poll revents=0x%X\r\n", pfd.revents);
                break;
            }
            ssize_t received = zsock_recv(state.tcp_fd, tcp_rx_chunk,
                                          sizeof(tcp_rx_chunk), 0);
            if (received == 0) {
                printk("Server closed TCP socket\r\n");
                if (server_response_buffer_len > 0U) {
                    if (transport_buffers.response[0] == 0xABU) {
                        printk("Connection closed before binary server packet completed\r\n");
                    } else {
                        printk("Connection closed with an incomplete server response buffered\r\n");
                    }
                    server_response_buffer_len = 0U;
                }
                tcp_disconnect();
                break;
            }
            if (received < 0) {
                printk("TCP RX recv failed: errno=%d\r\n", errno);
                break;
            }

            /* Receive never holds the transport mutex.  The short critical
             * section below serializes parser state and atomic settings apply
             * with CLI/TX work, without making zsock_recv() contend on it. */
            (void)k_mutex_lock(&micro_transport_mutex, K_FOREVER);
            printk("TCP RX chunk (%d bytes)\r\n", (int)received);
            int processed = process_server_response_bytes(tcp_rx_chunk,
                                                          (size_t)received, "tcp");
            if (processed < 0) {
                printk("TCP RX parser failed: %d\r\n", processed);
            }
            (void)k_mutex_unlock(&micro_transport_mutex);
        }
    }
}

static int tcp_send_hex_payload(const char *hex)
{
    if (!state.tcp_connected || state.tcp_fd < 0) {
        printk("TCP not connected. Use: connect <ip> <port>\r\n");
        return -ENOTCONN;
    }

    uint8_t *bin = transport_buffers.binary;
    size_t bin_len = 0;
    int err = hex_to_bytes(hex, bin, sizeof(transport_buffers.binary), &bin_len);
    if (err) {
        printk("Invalid hex string; error=%d\r\n", err);
        return err;
    }

    if (state.mode == WIRE_ASCII_HEX) {
        size_t len = strlen(hex);
        const char *p = hex;
        while (len > 0) {
            ssize_t n = zsock_send(state.tcp_fd, p, len, 0);
            if (n < 0) {
                printk("TCP send failed: errno=%d\r\n", errno);
                tcp_disconnect();
                return -errno;
            }
            p += n;
            len -= (size_t)n;
        }
        printk("Sent ASCII hex (%u chars)\r\n", (unsigned int)strlen(hex));
    } else {
        size_t remaining = bin_len;
        uint8_t *p = bin;
        while (remaining > 0) {
            ssize_t n = zsock_send(state.tcp_fd, p, remaining, 0);
            if (n < 0) {
                printk("TCP send failed: errno=%d\r\n", errno);
                tcp_disconnect();
                return -errno;
            }
            p += n;
            remaining -= (size_t)n;
        }
        printk("Sent binary packet (%u bytes)\r\n", (unsigned int)bin_len);
    }

    return 0;
}


static const char *transport_name(void)
{
    return state.transport == TRANSPORT_LTE_TCP ? "lte" : "relay";
}

static void relay_disconnect(void)
{
    if (state.relay_connected) {
        printk("MICRO_RELAY_DISCONNECT\r\n");
    }
    state.relay_connected = false;
    (void)k_timer_stop(&micro_heartbeat_timer);
    heartbeat_timer_armed = false;
    printk("Relay disconnected\r\n");
}

static int relay_connect_to_server(const char *ip, uint16_t port)
{
    if (ip == NULL || ip[0] == '\0') {
        return -EINVAL;
    }

    strncpy(state.server_ip, ip, sizeof(state.server_ip) - 1);
    state.server_ip[sizeof(state.server_ip) - 1] = '\0';
    state.server_port = port;
    state.relay_connected = false;

    printk("MICRO_RELAY_CONNECT:%s:%u\r\n", state.server_ip, state.server_port);
    printk("Relay connection requested. Wait for MICRO_RELAY_CONNECTED.\r\n");
    return 0;
}

static int relay_send_hex_payload(const char *hex)
{
    uint8_t *bin = transport_buffers.binary;
    size_t bin_len = 0;
    int err = hex_to_bytes(hex, bin, sizeof(transport_buffers.binary), &bin_len);
    if (err) {
        printk("Invalid hex string; error=%d\r\n", err);
        return err;
    }

    if (!state.relay_connected) {
        printk("Relay is not connected. Run: connect <ip> <port>\r\n");
        return -ENOTCONN;
    }

    if (state.mode == WIRE_ASCII_HEX) {
        printk("MICRO_RELAY_TX_ASCII_HEX:%s\r\n", hex);
        printk("Queued ASCII hex for Windows relay (%u chars)\r\n",
            (unsigned int)strlen(hex));
    } else {
        printk("MICRO_RELAY_TX_BINARY_HEX:%s\r\n", hex);
        printk("Queued binary packet for Windows relay (%u bytes)\r\n",
            (unsigned int)bin_len);
    }

    return 0;
}

static int transport_send_hex_payload(const char *hex)
{
    if (state.transport == TRANSPORT_SERIAL_RELAY) {
        return relay_send_hex_payload(hex);
    }

    if (!state.lte_registered) {
        printk("LTE is not connected. Run: lte connect, or use: transport relay\r\n");
        return -ENETDOWN;
    }

    return tcp_send_hex_payload(hex);
}


static bool transport_is_connected(void)
{
    return state.transport == TRANSPORT_SERIAL_RELAY ? state.relay_connected : state.tcp_connected;
}

static void log_thread_stack_usage(const char *phase, k_tid_t thread)
{
    size_t unused;
    if (thread == NULL) {
        return;
    }

    const char *name = k_thread_name_get(thread);
    if (name == NULL || name[0] == '\0') {
        name = "unnamed";
    }

    if (k_thread_stack_space_get(thread, &unused) == 0) {
        size_t size = thread->stack_info.size;
        printk("STACK %s: %s unused=%u used=%u size=%u\r\n",
               phase, name, (unsigned int)unused,
               (unsigned int)(size - unused), (unsigned int)size);
    } else {
        printk("STACK %s: %s unavailable\r\n", phase, name);
    }
}

static void log_relevant_stack_usage(const char *phase)
{
    printk("STACK SNAPSHOT: %s\r\n", phase);
    log_thread_stack_usage(phase, micro_main_thread);
    log_thread_stack_usage(phase, micro_heartbeat);
    log_thread_stack_usage(phase, micro_state_machine);
    log_thread_stack_usage(phase, micro_lte_location);
    log_thread_stack_usage(phase, micro_tcp_rx);
    log_thread_stack_usage(phase, k_work_queue_thread_get(&k_sys_work_q));

}

static void schedule_next_heartbeat(void)
{
    (void)k_timer_stop(&micro_heartbeat_timer);
    heartbeat_timer_armed = false;
    if (state.operation_mode != SIMULATOR_RUNTIME_MODE || !transport_is_connected()) {
        return;
    }
    uint32_t interval = active_config.heartbeat_interval_seconds;
    (void)k_timer_start(&micro_heartbeat_timer, K_SECONDS(interval), K_NO_WAIT);
    heartbeat_timer_armed = true;
    printk("Next automatic heartbeat scheduled in %u seconds\r\n", interval);
}

static void heartbeat_timer_expiry(struct k_timer *timer)
{
    ARG_UNUSED(timer);
    heartbeat_timer_armed = false;
    k_sem_give(&micro_heartbeat_trigger);
}

static void schedule_ble_checks(void)
{
    (void)k_timer_stop(&micro_ble_check_timer);
    ble_check_timer_armed = false;
    if (state.operation_mode != SIMULATOR_RUNTIME_MODE) {
        return;
    }
    uint32_t interval = active_config.ble_check_interval_seconds;
    (void)k_timer_start(&micro_ble_check_timer, K_SECONDS(interval), K_SECONDS(interval));
    ble_check_timer_armed = true;
    printk("BLE context reevaluation scheduled every %u seconds\r\n", interval);
}

static void schedule_lte_location_updates(bool send_now)
{
    (void)k_timer_stop(&micro_lte_location_timer);
    lte_location_timer_armed = false;
    if (state.operation_mode != SIMULATOR_RUNTIME_MODE ||
        state.tracking_state != DEVICE_STATE_GPS_LTE_OUTSIDE) {
        return;
    }
    uint32_t interval = active_config.lte_update_interval_seconds;
    (void)k_timer_start(&micro_lte_location_timer, K_SECONDS(interval), K_SECONDS(interval));
    lte_location_timer_armed = true;
    printk("LTE location updates scheduled every %u seconds while outside\r\n", interval);
    if (send_now) {
        k_sem_give(&micro_lte_location_trigger);
    }
}

static void request_state_reevaluation(void)
{
    if (state.operation_mode == SIMULATOR_RUNTIME_MODE) {
        k_sem_give(&micro_state_reevaluation_trigger);
    }
}

static void ble_check_timer_expiry(struct k_timer *timer)
{
    ARG_UNUSED(timer);
    /* BLE expiry changes only local awareness.  It does not directly send a
     * heartbeat or location packet. */
    request_state_reevaluation();
}

static void lte_location_timer_expiry(struct k_timer *timer)
{
    ARG_UNUSED(timer);
    if (state.operation_mode == SIMULATOR_RUNTIME_MODE &&
        state.tracking_state == DEVICE_STATE_GPS_LTE_OUTSIDE) {
        k_sem_give(&micro_lte_location_trigger);
    }
}

static bool detected_id_is_configured(const uint8_t id[MICRO_DEVICE_ID_BYTES],
                                      const uint8_t configured[][MICRO_DEVICE_ID_BYTES],
                                      uint8_t configured_count)
{
    for (uint8_t i = 0U; i < configured_count; ++i) {
        if (memcmp(id, configured[i], MICRO_DEVICE_ID_BYTES) == 0) {
            return true;
        }
    }
    return false;
}

static bool current_position_is_inside_safe_zone(void)
{
    /* The simulator uses a local planar metres approximation.  It is adequate
     * for the supplied zone radii and keeps the firmware independent of a
     * floating-point geodesy library. */
    for (uint8_t i = 0U; i < active_config.zone_count; ++i) {
        const struct micro_safe_zone *zone = &active_config.zones[i];
        int64_t north_m = ((int64_t)state.lat_e6 - zone->latitude_e6) * 111320LL / 1000000LL;
        int64_t east_m = ((int64_t)state.lon_e6 - zone->longitude_e6) * 111320LL / 1000000LL;
        int64_t radius_m = zone->radius_m;
        if ((north_m * north_m) + (east_m * east_m) <= radius_m * radius_m) {
            return true;
        }
    }
    return false;
}

static enum device_tracking_state evaluate_device_tracking_state(void)
{
    if (state.beacon_detected &&
        detected_id_is_configured(state.beacon_mac, active_config.beacons,
                                  active_config.beacon_count)) {
        return DEVICE_STATE_BEACON;
    }
    if (state.trusted_device_detected &&
        detected_id_is_configured(state.trusted_addr, active_config.trusted_devices,
                                  active_config.trusted_device_count)) {
        return DEVICE_STATE_TRUSTED_DEVICE;
    }
    /* The simulated position is the latest valid GNSS fix.  Only BLE failure
     * reaches this point, so the heartbeat timer never initiates GNSS work. */
    state.location_fix_age_seconds = 0U;
    return current_position_is_inside_safe_zone() ? DEVICE_STATE_GPS_SAFE_ZONE
                                                  : DEVICE_STATE_GPS_LTE_OUTSIDE;
}

static uint8_t heartbeat_opcode_for_tracking_state(enum device_tracking_state tracking_state)
{
    switch (tracking_state) {
    case DEVICE_STATE_BEACON: return OPCODE_BEACON;
    case DEVICE_STATE_TRUSTED_DEVICE: return OPCODE_TRUSTED;
    case DEVICE_STATE_GPS_SAFE_ZONE: return OPCODE_GPS_SAFEZONE;
    case DEVICE_STATE_GPS_LTE_OUTSIDE: return OPCODE_GPS_LTE;
    default: return OPCODE_GPS_LTE;
    }
}

static void reevaluate_device_state(void)
{
    if (state.operation_mode != SIMULATOR_RUNTIME_MODE) {
        return;
    }
    enum device_tracking_state previous = state.tracking_state;
    enum device_tracking_state next = evaluate_device_tracking_state();
    state.tracking_state = next;
    state.opcode = heartbeat_opcode_for_tracking_state(next);
    if (next != previous) {
        printk("State transition: %u -> %u; heartbeat opcode 0x%02X\r\n",
               previous, next, state.opcode);
    }
    if (previous != DEVICE_STATE_GPS_LTE_OUTSIDE && next == DEVICE_STATE_GPS_LTE_OUTSIDE) {
        schedule_lte_location_updates(true);
    } else if (previous == DEVICE_STATE_GPS_LTE_OUTSIDE && next != DEVICE_STATE_GPS_LTE_OUTSIDE) {
        schedule_lte_location_updates(false);
    }
}

static void state_machine_thread(void *p1, void *p2, void *p3)
{
    ARG_UNUSED(p1);
    ARG_UNUSED(p2);
    ARG_UNUSED(p3);
    (void)k_thread_name_set(k_current_get(), "micro_state");
    while (true) {
        (void)k_sem_take(&micro_state_reevaluation_trigger, K_FOREVER);
        (void)k_mutex_lock(&micro_transport_mutex, K_FOREVER);
        reevaluate_device_state();
        (void)k_mutex_unlock(&micro_transport_mutex);
    }
}

static void lte_location_thread(void *p1, void *p2, void *p3)
{
    ARG_UNUSED(p1);
    ARG_UNUSED(p2);
    ARG_UNUSED(p3);
    (void)k_thread_name_set(k_current_get(), "micro_lte_location");
    while (true) {
        (void)k_sem_take(&micro_lte_location_trigger, K_FOREVER);
        (void)k_mutex_lock(&micro_transport_mutex, K_FOREVER);
        if (state.operation_mode == SIMULATOR_RUNTIME_MODE &&
            state.tracking_state == DEVICE_STATE_GPS_LTE_OUTSIDE) {
            int err = build_location_hex(transport_buffers.hex, sizeof(transport_buffers.hex));
            if (err == 0) {
                printk("Automatic LTE location sent: %s\r\n", transport_buffers.hex);
                err = transport_send_hex_payload(transport_buffers.hex);
            }
            if (err != 0) {
                printk("Automatic LTE location failed: %d\r\n", err);
            }
        }
        (void)k_mutex_unlock(&micro_transport_mutex);
    }
}

static void heartbeat_thread(void *p1, void *p2, void *p3)
{
    ARG_UNUSED(p1);
    ARG_UNUSED(p2);
    ARG_UNUSED(p3);

    (void)k_thread_name_set(k_current_get(), "micro_heartbeat");

    while (true) {
        (void)k_sem_take(&micro_heartbeat_trigger, K_FOREVER);
        if (state.operation_mode != SIMULATOR_RUNTIME_MODE || !transport_is_connected()) {
            continue;
        }

        (void)k_mutex_lock(&micro_transport_mutex, K_FOREVER);
        log_relevant_stack_usage("micro_heartbeat-before");

        int err = build_heartbeat_hex(transport_buffers.hex,
                                      sizeof(transport_buffers.hex));
        if (err == 0) {
            printk("Automatic heartbeat sent: %s\r\n", transport_buffers.hex);
            err = transport_send_hex_payload(transport_buffers.hex);
        }
        if (err != 0) {
            printk("Automatic heartbeat failed: %d\r\n", err);
        }

        /* TCP responses are received independently by micro_tcp_rx. */
        log_relevant_stack_usage("micro_heartbeat-after");
        schedule_next_heartbeat();
        (void)k_mutex_unlock(&micro_transport_mutex);
    }
}

static bool handle_relay_control_line(const char *line)
{
    static const char connected_prefix[] = "MICRO_RELAY_CONNECTED:";
    static const char rx_prefix[] = "MICRO_RELAY_RX_HEX:";
    static const char error_prefix[] = "MICRO_RELAY_RX_ERROR:";

    if (strncmp(line, connected_prefix, strlen(connected_prefix)) == 0) {
        state.relay_connected = true;
        printk("Windows relay connected to %s\r\n", line + strlen(connected_prefix));
        schedule_next_heartbeat();
        return true;
    }

    if (strcmp(line, "MICRO_RELAY_DISCONNECTED") == 0) {
        state.relay_connected = false;
        printk("Windows relay disconnected\r\n");
        return true;
    }

    if (strcmp(line, "MICRO_RELAY_RX_EMPTY") == 0) {
        printk("Relay server returned no response\r\n");
        return true;
    }

    if (strncmp(line, error_prefix, strlen(error_prefix)) == 0) {
        state.relay_connected = false;
        printk("Relay error: %s\r\n", line + strlen(error_prefix));
        return true;
    }

    if (strncmp(line, rx_prefix, strlen(rx_prefix)) == 0) {
        const char *hex = line + strlen(rx_prefix);
        size_t resp_len = 0;
        int err = hex_to_bytes(hex, transport_buffers.binary,
                               sizeof(transport_buffers.binary), &resp_len);
        if (err) {
            printk("Invalid relay response HEX: %d\r\n", err);
        } else {
            int processed = process_server_response_bytes(transport_buffers.binary,
                                                          resp_len, "relay");
            if (processed < 0) {
                printk("Relay response parser failed: %d\r\n", processed);
            }
        }
        return true;
    }

    return false;
}

static int parse_scaled_i32(const char *s, int32_t scale, int32_t *out)
{
    bool neg = false;
    int64_t whole = 0;
    int64_t frac = 0;
    int64_t frac_scale = 1;
    const char *p = s;

    if (*p == '-') {
        neg = true;
        p++;
    } else if (*p == '+') {
        p++;
    }

    if (!isdigit((unsigned char)*p)) {
        return -EINVAL;
    }

    while (isdigit((unsigned char)*p)) {
        whole = (whole * 10) + (*p - '0');
        p++;
    }

    if (*p == '.') {
        p++;
        while (isdigit((unsigned char)*p) && frac_scale < scale) {
            frac = (frac * 10) + (*p - '0');
            frac_scale *= 10;
            p++;
        }
        while (isdigit((unsigned char)*p)) {
            p++;
        }
    }

    if (*p != '\0') {
        return -EINVAL;
    }

    int64_t value = (whole * scale) + ((frac * scale) / frac_scale);
    if (neg) {
        value = -value;
    }

    if (value > INT32_MAX || value < INT32_MIN) {
        return -ERANGE;
    }

    *out = (int32_t)value;
    return 0;
}

static int parse_u64(const char *s, uint64_t *out)
{
    if (s == NULL || s[0] == '\0' || s[0] == '-') {
        return -EINVAL;
    }

    errno = 0;
    char *end = NULL;
    unsigned long long v = strtoull(s, &end, 10);
    if (errno == ERANGE || end == s || *end != '\0') {
        return -EINVAL;
    }

    *out = (uint64_t)v;
    return 0;
}

static int parse_u32(const char *s, uint32_t *out)
{
    uint64_t value;
    if (parse_u64(s, &value) || value > UINT32_MAX) {
        return -EINVAL;
    }
    *out = (uint32_t)value;
    return 0;
}

static int parse_u16(const char *s, uint16_t *out)
{
    uint64_t value;
    if (parse_u64(s, &value) || value > UINT16_MAX) {
        return -EINVAL;
    }
    *out = (uint16_t)value;
    return 0;
}

static int parse_u8(const char *s, uint8_t *out)
{
    uint16_t tmp;
    int err = parse_u16(s, &tmp);
    if (err || tmp > 255) {
        return -EINVAL;
    }
    *out = (uint8_t)tmp;
    return 0;
}

static int parse_hex6(const char *s, uint8_t out[6])
{
    size_t len = strlen(s);
    if (len != 12) {
        return -EINVAL;
    }

    for (size_t i = 0; i < 6; i++) {
        int hi = hex_nibble(s[i * 2]);
        int lo = hex_nibble(s[(i * 2) + 1]);
        if (hi < 0 || lo < 0) {
            return -EINVAL;
        }
        out[i] = (uint8_t)((hi << 4) | lo);
    }

    return 0;
}

static const char *mode_name(void)
{
    return state.mode == WIRE_ASCII_HEX ? "ascii_hex" : "binary";
}

static const char *opcode_name(uint8_t opcode)
{
    switch (opcode) {
    case OPCODE_BEACON: return "beacon";
    case OPCODE_GPS_SAFEZONE: return "gps_safezone";
    case OPCODE_GPS_LTE: return "gps_lte";
    case OPCODE_TRUSTED: return "trusted";
    default: return "unknown";
    }
}

static const char *softsim_status_name(void)
{
    return state.softsim_provisioned ? "provisioned" : "not provisioned";
}

static const char *lte_status_name(void)
{
    if (state.lte_registered) {
        return "connected";
    }
    if (state.lte_connecting) {
        return "connecting";
    }
    return "disconnected";
}

static void print_help(void)
{
    printk("\r\nMicro simulator commands:\r\n");
    printk("  help\r\n");
    printk("  status\r\n");
    printk("  transport lte|relay\r\n");
    printk("  softsim status\r\n");
    printk("  softsim provision\r\n");
    printk("  lte status\r\n");
    printk("  lte connect\r\n");
    printk("  connect [ip] [port]\r\n");
    printk("  disconnect\r\n");
    printk("  recv\r\n");
    printk("  mode ascii_hex|binary\r\n");
    printk("  send sample_hb_beacon\r\n");
    printk("  send sample_hb_gps\r\n");
    printk("  send sample_location\r\n");
    printk("  send hb\r\n");
    printk("  send loc\r\n");
    printk("  send raw <HEX>\r\n");
    printk("  config show\r\n");
    printk("  config generate <setting_name> <value>\r\n");
    printk("  config apply <complete_packet_hex>\r\n");
    printk("  config set <setting_name> <value>\r\n");
    printk("  config reset confirm\r\n");
    printk("  config last\r\n");
    printk("  simulation status|on|off\r\n");
    printk("  preset normal|low|charging|outside|beacon|trusted\r\n");
    printk("  set battery low|medium|high\r\n");
    printk("  set charging on|off\r\n");
    printk("  set opcode beacon|trusted|gps_lte|gps_safezone (diagnostic only)\r\n");
    printk("  set pos <lat> <lon> <accuracy_m> <speed_mps>\r\n");
    printk("  set imei <digits>\r\n");
    printk("  set lastupdate <minutes>\r\n");
    printk("  set timestamp <unix_ms>\r\n");
    printk("  set time_mode fixed|running|step\r\n");
    printk("  set time_step <milliseconds>\r\n");
    printk("  set fixage <seconds>\r\n");
    printk("  set versions <sw> <fw>\r\n");
    printk("  set beacon <12_hex_chars>|off\r\n");
    printk("  set trusted <12_hex_chars>|off\r\n\r\n");
}

static void print_hex6(const uint8_t v[6])
{
    for (size_t i = 0; i < 6; i++) {
        printk("%02X", v[i]);
    }
}

static void print_coordinate_e6(int32_t value)
{
    int32_t magnitude = value < 0 ? -value : value;
    printk("%s%d.%06d", value < 0 ? "-" : "", magnitude / 1000000,
           magnitude % 1000000);
}

static void print_status(void)
{
    printk("\r\nSimulator status (non-secret state only):\r\n");
    printk("  transport: %s\r\n", transport_name());
    printk("  SoftSIM: %s\r\n", softsim_status_name());
    printk("  LTE: %s\r\n", lte_status_name());
    printk("  TCP connection: %s\r\n", state.tcp_connected ? "connected" : "disconnected");
    printk("  Windows relay connection: %s\r\n", state.relay_connected ? "connected" : "disconnected");
    printk("  TCP server: %s:%u\r\n", state.server_ip, state.server_port);
    printk("  wire mode: %s\r\n", mode_name());
    printk("  current sequence ID: %u\r\n", state.sequence_id);
    printk("  imei: %s\r\n", state.imei);
    printk("  software version: %u; firmware version: %u\r\n", state.sw_version, state.fw_version);
    printk("  last successfully applied update ID: %u\r\n", active_config.last_update_id);
    printk("  lastUpdate: %u minutes\r\n", state.last_update);
    printk("  battery state: 0x%02X; charging state: 0x%02X\r\n", state.battery, state.charging);
    printk("  operation mode: %s\r\n",
           state.operation_mode == SIMULATOR_RUNTIME_MODE ? "runtime simulation" : "configuration only (default)");
    printk("  current runtime state: %s\r\n",
           state.tracking_state == DEVICE_STATE_BEACON ? "BEACON" :
           state.tracking_state == DEVICE_STATE_TRUSTED_DEVICE ? "TRUSTED_DEVICE" :
           state.tracking_state == DEVICE_STATE_GPS_SAFE_ZONE ? "GPS_SAFE_ZONE" : "GPS_LTE_OUTSIDE");
    printk("  current heartbeat opcode: 0x%02X (%s)\r\n", state.opcode, opcode_name(state.opcode));
    printk("  current simulated time: %llu ms since Unix epoch (UTC)\r\n",
           (unsigned long long)simulated_current_time_ms());
    printk("  time simulation: %s; step=%llu ms; location fix age=%u s\r\n",
           timestamp_mode_name(),
           (unsigned long long)state.timestamp_step_ms,
           state.location_fix_age_seconds);
    printk("  currently detected beacon: %s", state.beacon_detected ? "present " : "absent");
    if (state.beacon_detected) { print_hex6(state.beacon_mac); }
    printk("\r\n");
    printk("  currently detected trusted device: %s", state.trusted_device_detected ? "present " : "absent");
    if (state.trusted_device_detected) { print_hex6(state.trusted_addr); }
    printk("\r\n");
    printk("  latest GPS location: latitude="); print_coordinate_e6(state.lat_e6);
    printk(" longitude="); print_coordinate_e6(state.lon_e6);
    printk(" accuracy_m=%u.%u speed_mps=%u.%u\r\n", state.accuracy_x10 / 10U,
           state.accuracy_x10 % 10U, state.speed_x10 / 10U, state.speed_x10 % 10U);
    printk("  heartbeat interval: %u seconds; timer=%s\r\n", active_config.heartbeat_interval_seconds,
           heartbeat_timer_armed ? "scheduled" : "stopped");
    printk("  BLE check interval: %u seconds; timer=%s\r\n", active_config.ble_check_interval_seconds,
           ble_check_timer_armed ? "scheduled" : "stopped");
    printk("  LTE update interval: %u seconds; outside timer=%s\r\n", active_config.lte_update_interval_seconds,
           lte_location_timer_armed ? "scheduled" : "stopped");
    printk("  SendingUpdate: 0x%02X (%s)\r\n", active_config.sending_update,
           active_config.sending_update == 0xFFU ? "firmware update indicated" : "no firmware update");
    printk("  safe zones: %u\r\n", active_config.zone_count);
    for (uint8_t i = 0U; i < active_config.zone_count; ++i) {
        printk("    safe zone %u: latitude=", i + 1U); print_coordinate_e6(active_config.zones[i].latitude_e6);
        printk(" longitude="); print_coordinate_e6(active_config.zones[i].longitude_e6);
        printk(" radius_m=%u\r\n", active_config.zones[i].radius_m);
    }
    printk("  configured fixed beacons: %u\r\n", active_config.beacon_count);
    for (uint8_t i = 0U; i < active_config.beacon_count; ++i) {
        printk("    beacon %u: ", i + 1U); print_hex6(active_config.beacons[i]); printk("\r\n");
    }
    printk("  configured trusted devices: %u\r\n", active_config.trusted_device_count);
    for (uint8_t i = 0U; i < active_config.trusted_device_count; ++i) {
        printk("    trusted device %u: ", i + 1U); print_hex6(active_config.trusted_devices[i]); printk("\r\n");
    }
    printk("\r\n");
}

static void apply_preset(const char *preset)
{
    if (strcmp(preset, "normal") == 0) {
        state.battery = 0x01;
        state.charging = 0x01;
        state.beacon_detected = true;
        state.trusted_device_detected = true;
    } else if (strcmp(preset, "low") == 0) {
        state.battery = 0x00;
        state.charging = 0x01;
    } else if (strcmp(preset, "charging") == 0) {
        state.battery = 0x01;
        state.charging = 0x10;
    } else if (strcmp(preset, "outside") == 0) {
        state.beacon_detected = false;
        state.trusted_device_detected = false;
        state.lat_e6 = 45450000;
        state.lon_e6 = -75750000;
        state.accuracy_x10 = 300;
        state.speed_x10 = 12;
    } else if (strcmp(preset, "beacon") == 0) {
        state.beacon_detected = true;
        state.trusted_device_detected = false;
    } else if (strcmp(preset, "trusted") == 0) {
        state.beacon_detected = false;
        state.trusted_device_detected = true;
    } else {
        printk("Unknown preset: %s\r\n", preset);
        return;
    }

    printk("Applied preset: %s\r\n", preset);
    request_state_reevaluation();
}

static char *trim_left(char *s)
{
    while (s && isspace((unsigned char)*s)) {
        s++;
    }
    return s;
}

static void split_first(char *line, char **first, char **rest)
{
    line = trim_left(line);
    *first = line;
    *rest = NULL;

    if (line == NULL || line[0] == '\0') {
        *first = NULL;
        return;
    }

    char *space = line;
    while (*space != '\0' && !isspace((unsigned char)*space)) {
        space++;
    }

    if (*space != '\0') {
        *space = '\0';
        *rest = trim_left(space + 1);
    }
}

static void handle_send_command(char *args)
{
    char *what = NULL;
    char *rest = NULL;
    char *hex = transport_buffers.hex;

    split_first(args, &what, &rest);

    if (what == NULL) {
        printk("Usage: send <sample_hb_beacon|sample_hb_gps|sample_location|hb|loc|raw>\r\n");
        return;
    }

    if (strcmp(what, "sample_hb_beacon") == 0) {
        transport_send_hex_payload(SAMPLE_HB_BEACON);
    } else if (strcmp(what, "sample_hb_gps") == 0) {
        transport_send_hex_payload(SAMPLE_HB_GPS);
    } else if (strcmp(what, "sample_location") == 0) {
        transport_send_hex_payload(SAMPLE_LOCATION);
    } else if (strcmp(what, "hb") == 0) {
        int err = build_heartbeat_hex(hex, sizeof(transport_buffers.hex));
        if (err) {
            printk("Failed to build heartbeat packet: %d\r\n", err);
            return;
        }
        printk("Dynamic heartbeat hex: %s\r\n", hex);
        transport_send_hex_payload(hex);
    } else if (strcmp(what, "loc") == 0) {
        int err = build_location_hex(hex, sizeof(transport_buffers.hex));
        if (err) {
            printk("Failed to build location packet: %d\r\n", err);
            return;
        }
        printk("Dynamic location hex: %s\r\n", hex);
        transport_send_hex_payload(hex);
    } else if (strcmp(what, "raw") == 0) {
        if (rest == NULL || rest[0] == '\0') {
            printk("Usage: send raw <HEX>\r\n");
            return;
        }
        transport_send_hex_payload(rest);
    } else {
        printk("Unknown send target: %s\r\n", what);
    }
}

static void handle_set_command(char *args)
{
    char *field = strtok(args, " ");

    if (field == NULL) {
        printk("Usage: set <field> <value>\r\n");
        return;
    }

    if (strcmp(field, "battery") == 0) {
        char *v = strtok(NULL, " ");
        if (!v) { printk("Usage: set battery low|medium|high\r\n"); return; }
        if (strcmp(v, "low") == 0) state.battery = 0x00;
        else if (strcmp(v, "medium") == 0) state.battery = 0x01;
        else if (strcmp(v, "high") == 0) state.battery = 0x10;
        else { printk("Invalid battery value\r\n"); return; }
        printk("battery set\r\n");
    } else if (strcmp(field, "charging") == 0) {
        char *v = strtok(NULL, " ");
        if (!v) { printk("Usage: set charging on|off\r\n"); return; }
        if (strcmp(v, "on") == 0) state.charging = 0x10;
        else if (strcmp(v, "off") == 0) state.charging = 0x01;
        else { printk("Invalid charging value\r\n"); return; }
        printk("charging set\r\n");
    } else if (strcmp(field, "opcode") == 0) {
        char *v = strtok(NULL, " ");
        if (!v) { printk("Usage: set opcode beacon|trusted|gps_lte|gps_safezone (diagnostic only)\r\n"); return; }
        if (strcmp(v, "beacon") == 0) { state.opcode = OPCODE_BEACON; state.tracking_state = DEVICE_STATE_BEACON; }
        else if (strcmp(v, "trusted") == 0) { state.opcode = OPCODE_TRUSTED; state.tracking_state = DEVICE_STATE_TRUSTED_DEVICE; }
        else if (strcmp(v, "gps_lte") == 0) { state.opcode = OPCODE_GPS_LTE; state.tracking_state = DEVICE_STATE_GPS_LTE_OUTSIDE; }
        else if (strcmp(v, "gps_safezone") == 0) { state.opcode = OPCODE_GPS_SAFEZONE; state.tracking_state = DEVICE_STATE_GPS_SAFE_ZONE; }
        else { printk("Invalid opcode\r\n"); return; }
        printk("diagnostic opcode set to %s; runtime simulation will derive it at the next BLE check\r\n",
               opcode_name(state.opcode));
    } else if (strcmp(field, "pos") == 0) {
        char *lat = strtok(NULL, " ");
        char *lon = strtok(NULL, " ");
        char *acc = strtok(NULL, " ");
        char *spd = strtok(NULL, " ");
        int32_t lat_e6, lon_e6, acc_x10, spd_x10;
        if (!lat || !lon || !acc || !spd) {
            printk("Usage: set pos <lat> <lon> <accuracy_m> <speed_mps>\r\n"); return;
        }
        if (parse_scaled_i32(lat, 1000000, &lat_e6) ||
            parse_scaled_i32(lon, 1000000, &lon_e6) ||
            parse_scaled_i32(acc, 10, &acc_x10) ||
            parse_scaled_i32(spd, 10, &spd_x10) ||
            lat_e6 < -90000000 || lat_e6 > 90000000 ||
            lon_e6 < -180000000 || lon_e6 > 180000000 ||
            acc_x10 < 0 || spd_x10 < 0 ||
            acc_x10 > 65535 || spd_x10 > 65535) {
            printk("Invalid pos values. Latitude must be -90..90, longitude -180..180, "
                   "and accuracy/speed must be non-negative.\r\n");
            return;
        }
        state.lat_e6 = lat_e6;
        state.lon_e6 = lon_e6;
        state.accuracy_x10 = (uint16_t)acc_x10;
        state.speed_x10 = (uint16_t)spd_x10;
        printk("position set\r\n");
        request_state_reevaluation();
    } else if (strcmp(field, "imei") == 0) {
        char *v = strtok(NULL, " ");
        if (!v || strlen(v) != 15) {
            printk("Usage: set imei <15_digits>\r\n");
            return;
        }
        for (size_t i = 0; i < 15; i++) {
            if (!isdigit((unsigned char)v[i])) {
                printk("IMEI must contain exactly 15 decimal digits\r\n");
                return;
            }
        }
        strncpy(state.imei, v, sizeof(state.imei) - 1);
        state.imei[sizeof(state.imei) - 1] = '\0';
        printk("imei set\r\n");
    } else if (strcmp(field, "lastupdate") == 0) {
        char *v = strtok(NULL, " ");
        uint16_t out;
        if (!v || parse_u16(v, &out)) { printk("Usage: set lastupdate <minutes>\r\n"); return; }
        state.last_update = out;
        printk("lastupdate set\r\n");
    } else if (strcmp(field, "timestamp") == 0) {
        char *v = strtok(NULL, " ");
        uint64_t timestamp_ms;
        if (!v || parse_u64(v, &timestamp_ms)) {
            printk("Usage: set timestamp <unix_ms>\r\n");
            return;
        }
        state.timestamp_base_ms = timestamp_ms;
        reset_timestamp_uptime_anchor();
        printk("timestamp set to %llu ms since Unix epoch UTC\r\n",
               (unsigned long long)state.timestamp_base_ms);
    } else if (strcmp(field, "time_mode") == 0) {
        char *v = strtok(NULL, " ");
        if (!v) {
            printk("Usage: set time_mode fixed|running|step\r\n");
            return;
        }

        uint64_t current_ms = simulated_current_time_ms();
        if (strcmp(v, "fixed") == 0) {
            state.timestamp_mode = TIMESTAMP_FIXED;
        } else if (strcmp(v, "running") == 0) {
            state.timestamp_mode = TIMESTAMP_RUNNING;
        } else if (strcmp(v, "step") == 0) {
            state.timestamp_mode = TIMESTAMP_STEP;
        } else {
            printk("Usage: set time_mode fixed|running|step\r\n");
            return;
        }
        state.timestamp_base_ms = current_ms;
        reset_timestamp_uptime_anchor();
        printk("time mode set to %s\r\n", timestamp_mode_name());
    } else if (strcmp(field, "time_step") == 0) {
        char *v = strtok(NULL, " ");
        uint64_t step_ms;
        if (!v || parse_u64(v, &step_ms) || step_ms == 0) {
            printk("Usage: set time_step <positive_milliseconds>\r\n");
            return;
        }
        state.timestamp_step_ms = step_ms;
        printk("timestamp step set to %llu ms per generated packet\r\n",
               (unsigned long long)state.timestamp_step_ms);
    } else if (strcmp(field, "fixage") == 0) {
        char *v = strtok(NULL, " ");
        uint32_t seconds;
        if (!v || parse_u32(v, &seconds)) {
            printk("Usage: set fixage <seconds>\r\n");
            return;
        }
        state.location_fix_age_seconds = seconds;
        printk("location fix age set to %u seconds\r\n",
               state.location_fix_age_seconds);
    } else if (strcmp(field, "versions") == 0) {
        char *sw = strtok(NULL, " ");
        char *fw = strtok(NULL, " ");
        uint8_t swv, fwv;
        if (!sw || !fw || parse_u8(sw, &swv) || parse_u8(fw, &fwv)) {
            printk("Usage: set versions <sw> <fw>\r\n"); return;
        }
        state.sw_version = swv;
        state.fw_version = fwv;
        printk("versions set\r\n");
    } else if (strcmp(field, "beacon") == 0) {
        char *v = strtok(NULL, " ");
        if (!v) { printk("Usage: set beacon <12_hex_chars>|off\r\n"); return; }
        if (strcmp(v, "off") == 0) {
            state.beacon_detected = false;
        } else if (parse_hex6(v, state.beacon_mac)) {
            printk("Usage: set beacon <12_hex_chars>|off\r\n"); return;
        } else {
            state.beacon_detected = true;
        }
        printk("beacon detection updated\r\n");
        request_state_reevaluation();
    } else if (strcmp(field, "trusted") == 0) {
        char *v = strtok(NULL, " ");
        if (!v) { printk("Usage: set trusted <12_hex_chars>|off\r\n"); return; }
        if (strcmp(v, "off") == 0) {
            state.trusted_device_detected = false;
        } else if (parse_hex6(v, state.trusted_addr)) {
            printk("Usage: set trusted <12_hex_chars>|off\r\n"); return;
        } else {
            state.trusted_device_detected = true;
        }
        printk("trusted-device detection updated\r\n");
        request_state_reevaluation();
    } else {
        printk("Unknown field: %s\r\n", field);
    }
}


static void print_persistent_config(void)
{
    printk("\r\nPersistent configuration:\r\n");
    printk("  target_device_imei: %s\r\n", state.imei);
    printk("  last_successful_update_id: %u\r\n", active_config.last_update_id);
    printk("  heartbeat_interval_seconds: %u (range 1..65535; default 60)\r\n",
           active_config.heartbeat_interval_seconds);
    printk("  lte_update_interval_seconds: %u (range 1..65535; default 480)\r\n",
           active_config.lte_update_interval_seconds);
    printk("  ble_check_interval_seconds: %u (range 1..65535; default 480)\r\n",
           active_config.ble_check_interval_seconds);
    printk("  legacy CLI alias: sleep_interval_seconds maps to ble_check_interval_seconds\r\n");
    printk("  sending_update: 0x%02X (%s)\r\n", active_config.sending_update,
           active_config.sending_update == 0xFFU ? "firmware update indicated" : "no firmware update");
    printk("  safe_zones: %u\r\n", active_config.zone_count);
    for (uint8_t i = 0U; i < active_config.zone_count; ++i) {
        printk("    %u: lat_e6=%d lon_e6=%d radius_m=%u\r\n", i + 1U,
               active_config.zones[i].latitude_e6,
               active_config.zones[i].longitude_e6,
               active_config.zones[i].radius_m);
    }
    printk("  beacons: %u\r\n", active_config.beacon_count);
    for (uint8_t i = 0U; i < active_config.beacon_count; ++i) {
        printk("    %u: ", i + 1U); print_hex6(active_config.beacons[i]); printk("\r\n");
    }
    printk("  trusted_devices: %u\r\n", active_config.trusted_device_count);
    for (uint8_t i = 0U; i < active_config.trusted_device_count; ++i) {
        printk("    %u: ", i + 1U); print_hex6(active_config.trusted_devices[i]); printk("\r\n");
    }
}

static void print_last_config_update(void)
{
    printk("\r\nLast configuration update:\r\n");
    printk("  update_id: %u\r\n", last_config_result.update_id);
    printk("  source: %s\r\n", last_config_source);
    printk("  uptime_ms: %u\r\n", last_config_uptime_ms);
    printk("  result: %s (%d)\r\n", last_config_status == 0 ? "success" : "failure",
           last_config_status);
    printk("  changed_count: %u\r\n", last_config_result.changed_count);
    for (uint8_t i = 0U; i < last_config_result.changed_count; ++i) {
        const struct micro_setting_definition *definition =
            micro_setting_by_id(last_config_result.changed_ids[i]);
        printk("    %u: 0x%02X %s\r\n", i + 1U,
               last_config_result.changed_ids[i],
               definition != NULL ? definition->name : "unknown");
    }
    if (last_config_error[0] != '\0') {
        printk("  validation_error: %s\r\n", last_config_error);
    }
}

static void handle_simulation_command(char *args)
{
    char *action = NULL;
    char *tail = NULL;
    split_first(args ? args : "", &action, &tail);
    if (action == NULL || strcmp(action, "status") == 0) {
        printk("Simulation mode: %s\r\n",
               state.operation_mode == SIMULATOR_RUNTIME_MODE ? "runtime enabled" : "configuration only");
        return;
    }
    if (strcmp(action, "on") == 0) {
        state.operation_mode = SIMULATOR_RUNTIME_MODE;
        schedule_ble_checks();
        schedule_next_heartbeat();
        request_state_reevaluation();
        printk("Runtime simulation enabled. BLE state is being evaluated now.\r\n");
        return;
    }
    if (strcmp(action, "off") == 0) {
        state.operation_mode = SIMULATOR_CONFIGURATION_MODE;
        (void)k_timer_stop(&micro_heartbeat_timer);
        (void)k_timer_stop(&micro_ble_check_timer);
        (void)k_timer_stop(&micro_lte_location_timer);
        heartbeat_timer_armed = false;
        ble_check_timer_armed = false;
        lte_location_timer_armed = false;
        printk("Runtime simulation disabled. Persistent configuration remains unchanged.\r\n");
        return;
    }
    printk("Usage: simulation status|on|off\r\n");
}

static void handle_config_command(char *args)
{
    char *action = NULL;
    char *rest = NULL;
    split_first(args ? args : "", &action, &rest);
    if (action == NULL || strcmp(action, "show") == 0) {
        print_persistent_config();
        return;
    }
    if (strcmp(action, "last") == 0) {
        print_last_config_update();
        return;
    }
    if (strcmp(action, "generate") == 0 || strcmp(action, "set") == 0) {
        char *name = NULL;
        char *value = NULL;
        char *tail = NULL;
        split_first(rest ? rest : "", &name, &tail);
        split_first(tail ? tail : "", &value, &tail);
        if (name == NULL || value == NULL || (tail != NULL && tail[0] != '\0')) {
            printk("Usage: config %s <setting_name> <value>\r\n", action);
            return;
        }
        char *hex = transport_buffers.hex;
        uint16_t update_id = 0U;
        int err = build_settings_update_hex(name, value, hex,
                                            sizeof(transport_buffers.hex), &update_id);
        if (err != 0) {
            printk("Could not generate settings packet: %d\r\n", err);
            return;
        }
        printk("Generated settings packet (update_id=%u): %s\r\n", update_id, hex);
        if (strcmp(action, "generate") == 0) {
            printk("Packet was not applied.\r\n");
            return;
        }
        uint8_t *packet = transport_buffers.packet;
        size_t packet_len = 0U;
        err = hex_to_bytes(hex, packet, MAX_PACKET_BYTES, &packet_len);
        if (err == 0) {
            err = apply_settings_packet_bytes(packet, packet_len, "serial");
        }
        if (err != 0) {
            printk("config set failed: %d\r\n", err);
        }
        return;
    }
    if (strcmp(action, "apply") == 0) {
        if (rest == NULL || rest[0] == '\0') {
            printk("Usage: config apply <complete_packet_hex>\r\n");
            return;
        }
        uint8_t *packet = transport_buffers.packet;
        size_t packet_len = 0U;
        int err = hex_to_bytes(rest, packet, MAX_PACKET_BYTES, &packet_len);
        if (err != 0) {
            printk("Malformed packet HEX: %d\r\n", err);
            return;
        }
        err = apply_settings_packet_bytes(packet, packet_len, "serial");
        if (err != 0) {
            printk("config apply failed: %d\r\n", err);
        }
        return;
    }
    if (strcmp(action, "reset") == 0) {
        if (rest == NULL || strcmp(rest, "confirm") != 0) {
            printk("Development reset requires: config reset confirm\r\n");
            return;
        }
        struct micro_persistent_config defaults;
        micro_settings_defaults(&defaults);
        char *hex = transport_buffers.hex;
        uint16_t update_id = 0U;
        int err = build_settings_config_update_hex(&defaults, hex,
                                                   sizeof(transport_buffers.hex),
                                                   &update_id);
        if (err != 0) {
            printk("Could not generate defaults update packet: %d\r\n", err);
            return;
        }
        printk("Generated defaults packet (update_id=%u): %s\r\n", update_id, hex);
        uint8_t *packet = transport_buffers.packet;
        size_t packet_len = 0U;
        err = hex_to_bytes(hex, packet, MAX_PACKET_BYTES, &packet_len);
        if (err == 0) {
            err = apply_settings_packet_bytes(packet, packet_len, "serial-reset");
        }
        if (err != 0) {
            printk("Could not restore configuration defaults: %d\r\n", err);
            return;
        }
        printk("Persistent configuration restored through the normal settings packet flow\r\n");
        return;
    }
    printk("Usage: config show|generate|apply|set|reset confirm|last\r\n");
}

static void handle_transport_command(char *args)
{
    char *value = NULL;
    char *tail = NULL;
    split_first(args ? args : "", &value, &tail);

    if (!value) {
        printk("Usage: transport lte|relay\r\n");
        return;
    }

    if (strcmp(value, "lte") == 0) {
        relay_disconnect();
        state.transport = TRANSPORT_LTE_TCP;
        printk("transport set to lte\r\n");
        if (!state.softsim_provisioned) {
            printk("SoftSIM is not provisioned. LTE transport cannot connect yet.\r\n");
        } else if (!state.lte_registered) {
            printk("Run: lte connect\r\n");
        }
    } else if (strcmp(value, "relay") == 0) {
        state.transport = TRANSPORT_SERIAL_RELAY;
        tcp_disconnect();
        printk("transport set to relay\r\n");
        printk("Run the Windows relay script, then use: connect <ip> <port>\r\n");
    } else {
        printk("Usage: transport lte|relay\r\n");
    }
}

static void handle_softsim_command(char *args)
{
    char *value = NULL;
    char *tail = NULL;
    split_first(args ? args : "", &value, &tail);

    if (!value || strcmp(value, "status") == 0) {
        printk("SoftSIM: %s\r\n", softsim_status_name());
        return;
    }

    if (strcmp(value, "provision") == 0) {
        if (state.softsim_provisioned) {
            printk("SoftSIM is already provisioned. Provisioning again is not required.\r\n");
            return;
        }

        int err = provision_softsim_from_serial();
        if (err) {
            printk("SoftSIM provisioning failed: %d\r\n", err);
        }
        return;
    }

    printk("Usage: softsim status|provision\r\n");
}

static void handle_lte_command(char *args)
{
    char *value = NULL;
    char *tail = NULL;
    split_first(args ? args : "", &value, &tail);

    if (!value || strcmp(value, "status") == 0) {
        printk("LTE: %s\r\n", lte_status_name());
        return;
    }

    if (strcmp(value, "connect") == 0) {
        int err = lte_connect_start();
        if (err) {
            printk("LTE connect could not start: %d\r\n", err);
        }
        return;
    }

    printk("Usage: lte status|connect\r\n");
}

static void handle_command(char *line)
{
    char *cmd = NULL;
    char *rest = NULL;

    if (handle_relay_control_line(line)) {
        return;
    }

    split_first(line, &cmd, &rest);

    if (cmd == NULL || cmd[0] == '\0') {
        return;
    }

    if (strcmp(cmd, "help") == 0) {
        print_help();
    } else if (strcmp(cmd, "status") == 0) {
        print_status();
    } else if (strcmp(cmd, "transport") == 0) {
        handle_transport_command(rest ? rest : "");
    } else if (strcmp(cmd, "softsim") == 0) {
        handle_softsim_command(rest ? rest : "");
    } else if (strcmp(cmd, "lte") == 0) {
        handle_lte_command(rest ? rest : "");
    } else if (strcmp(cmd, "connect") == 0) {
        char *ip = NULL;
        char *port_s = NULL;
        char *tail = NULL;
        uint16_t port = state.server_port;

        split_first(rest ? rest : "", &ip, &tail);
        split_first(tail ? tail : "", &port_s, &tail);

        if (!ip) {
            ip = state.server_ip;
        }
        if (port_s && parse_u16(port_s, &port)) {
            printk("Invalid port\r\n");
            return;
        }

        if (state.transport == TRANSPORT_SERIAL_RELAY) {
            relay_connect_to_server(ip, port);
        } else {
            if (!state.lte_registered) {
                printk("LTE is not connected. Run: lte connect\r\n");
                return;
            }
            tcp_connect_to_server(ip, port);
        }
    } else if (strcmp(cmd, "disconnect") == 0) {
        if (state.transport == TRANSPORT_SERIAL_RELAY) {
            relay_disconnect();
        } else {
            tcp_disconnect();
        }
    } else if (strcmp(cmd, "recv") == 0) {
        if (state.transport == TRANSPORT_SERIAL_RELAY) {
            printk("Relay responses are returned automatically by the Windows relay script.\r\n");
        } else {
            tcp_recv_response();
        }
    } else if (strcmp(cmd, "mode") == 0) {
        char *v = NULL;
        char *tail = NULL;
        split_first(rest ? rest : "", &v, &tail);
        if (!v) { printk("Usage: mode ascii_hex|binary\r\n"); return; }
        if (strcmp(v, "ascii_hex") == 0) state.mode = WIRE_ASCII_HEX;
        else if (strcmp(v, "binary") == 0) state.mode = WIRE_BINARY;
        else { printk("Invalid mode\r\n"); return; }
        printk("mode set to %s\r\n", mode_name());
    } else if (strcmp(cmd, "send") == 0) {
        handle_send_command(rest ? rest : "");
    } else if (strcmp(cmd, "preset") == 0) {
        char *p = NULL;
        char *tail = NULL;
        split_first(rest ? rest : "", &p, &tail);
        if (!p) { printk("Usage: preset normal|low|charging|outside|beacon|trusted\r\n"); return; }
        apply_preset(p);
    } else if (strcmp(cmd, "set") == 0) {
        handle_set_command(rest ? rest : "");
    } else if (strcmp(cmd, "config") == 0) {
        handle_config_command(rest ? rest : "");
    } else if (strcmp(cmd, "simulation") == 0) {
        handle_simulation_command(rest ? rest : "");
    } else {
        printk("Unknown command: %s. Type: help\r\n", cmd);
    }
}

static void cli_loop(void)
{
    char *line = cli_line;

    printk("\r\nSerial CLI ready. Type: help\r\n");
    printk("Transport: %s\r\n", transport_name());

    if (state.transport == TRANSPORT_SERIAL_RELAY) {
        printk("Start the Windows relay script, then run:\r\n");
        printk("  connect %s %u\r\n", state.server_ip, state.server_port);
    } else {
        printk("LTE connection starts in the background. Check with: lte status\r\n");
        printk("After LTE connects, run: connect %s %u\r\n",
            state.server_ip, state.server_port);
    }

    while (true) {
        printk("micro> ");
        int n = read_line_serial(line, sizeof(cli_line), true);
        if (n < 0) {
            printk("line read error: %d\r\n", n);
            continue;
        }
        (void)k_mutex_lock(&micro_transport_mutex, K_FOREVER);
        handle_command(line);
        (void)k_mutex_unlock(&micro_transport_mutex);
    }
}

int main(void)
{
    micro_main_thread = k_current_get();
    (void)k_thread_name_set(micro_main_thread, "main");
    printk("MICRO BUILD: heartbeat-worker-fix-v2\r\n");
    LOG_INF("Micro serial simulator started");

    if (calculate_crc16((const uint8_t *)"123456789", 9, 0x0000) != 0x31C3) {
        LOG_ERR("CRC-16/XMODEM self-test failed");
        return -EIO;
    }

    int err = serial_input_start();
    if (err) {
        return err;
    }

    last_config_status = -ENOENT;
    err = persistent_settings_init();
    if (err != 0) {
        LOG_ERR("Persistent simulator settings unavailable: %d", err);
        return err;
    }

    state.softsim_provisioned = nrf_softsim_check_provisioned();
    reset_timestamp_uptime_anchor();

    if (state.softsim_provisioned) {
        state.transport = TRANSPORT_LTE_TCP;
        LOG_INF("SoftSIM is provisioned");

        err = lte_connect_start();
        if (err) {
            LOG_WRN("LTE did not start. Serial relay mode remains available.");
        }
    } else {
        state.transport = TRANSPORT_SERIAL_RELAY;
        LOG_INF("SoftSIM is not provisioned");
        printk("\r\nThe simulator can run without SoftSIM using the Windows Wi-Fi relay.\r\n");
        printk("To provision SoftSIM later, run: softsim provision\r\n");
    }

    cli_loop();
    return 0;
}
