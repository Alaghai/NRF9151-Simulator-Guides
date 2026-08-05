cmake_minimum_required(VERSION 3.20.0)

# Include the Onomondo SoftSIM overlay from the workspace.
# This path assumes the app lives at:
#   <workspace>/applications/micro_simulator
# and the Onomondo SoftSIM module lives at:
#   <workspace>/modules/lib/onomondo-softsim
list(APPEND OVERLAY_CONFIG
    "${CMAKE_CURRENT_LIST_DIR}/../../modules/lib/onomondo-softsim/overlay-softsim.conf"
)

find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})

project(micro_simulator)

target_sources(app PRIVATE
    src/main.c
    src/micro_settings.c
)
