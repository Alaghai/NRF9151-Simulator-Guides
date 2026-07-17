MICRO SIMULATOR PROJECT README - VERSION 5

nRF9151 DK + Onomondo SoftSIM + Direct LTE or Windows Wi-Fi Relay

DOCUMENT SERIES
---------------
The previous SoftSIM provisioning guide and simulator clean-build guide have been combined into one setup guide. The documentation series is now three guides. The historical numbers 3 and 4 are retained to avoid breaking existing references:

1. 1_SoftSIM_and_Simulator_Setup_Guide_v5.docx
   Start-to-finish Windows setup, SDK/toolchain 3.4, Onomondo source workspace, optional SoftSIM profile generation, firmware installation/build/flash, profile provisioning, direct LTE, and Windows relay setup.

2. 3_Simulator_Protocol_Decisions_and_Usage_v4.docx
   Simulator packet decisions, commands, presets, and test scenarios.

3. 4_Simulator_TCP_Server_and_Command_Guide_v4.docx
   TCP server access, logging, service management, and response testing.

KEY SETUP PATHS
---------------
Relay only / no LTE-M coverage:
- Required: VS Code, nRF Connect extension, SDK and toolchain 3.4.x, Onomondo west source workspace, simulator install/build/flash, and Windows relay.
- Skip: C:\ss, SoftSIM CLI, RSA/API-key work, profile generation, profile provisioning, and LTE attach.

Cellular with an already-provisioned board:
- Skip all profile-generation and provisioning work.
- Build/flash and use transport lte where LTE-M is available.

Cellular with a profile supplied by Amir:
- Skip C:\ss and all CLI/key-generation steps.
- Save the supplied board-specific profile securely, run softsim provision, paste the full HEX profile once, and wait for reboot.

Cellular with no available profile:
- Complete the optional C:\ss SoftSIM CLI workflow to generate one unique profile.

IMPORTANT DISTINCTION
---------------------
The Onomondo nrf-softsim source integration inside the west workspace is required to compile the firmware for every path. The separate C:\ss folder and account/profile-generation workflow are optional unless a new cellular profile must be generated.

USER-INDEPENDENT PATHS
----------------------
Workspace:
  $Workspace = Join-Path $env:USERPROFILE "onomondo-softsim-test"

Firmware application:
  $env:USERPROFILE\onomondo-softsim-test\applications\micro_simulator

Windows relay tools:
  $env:USERPROFILE\onomondo-softsim-test\tools\micro_relay

Short build path:
  C:\b\micro_sim_relay

SECURITY
--------
Ask Amir privately for a board-specific SoftSIM HEX profile and server credentials when authorized. Do not commit API keys, RSA private keys, HEX profiles, or server passwords to Git or include them in shared documentation. One SoftSIM profile must be used for only one physical board.
