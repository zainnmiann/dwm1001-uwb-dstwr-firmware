# ---------------------------------------------------------------------------
# DWM1001-DEV  --  TDMA / DS-TWR RTLS firmware
#
#   make            build _build/dwm1001_tdma_twr.hex
#   make flash      program over J-Link (nrfjprog)
#   make flash-jl   program over J-Link (JLinkExe, no nrfjprog needed)
#   make size       section sizes
#   make clean
#
# Override the toolchain prefix if it is not on PATH, e.g.
#   make GNU_INSTALL_ROOT=/opt/arm-gnu-toolchain-13.2/bin/
# ---------------------------------------------------------------------------

PROJECT_NAME := dwm1001_tdma_twr
OUTPUT_DIR   := _build

# If arm-none-eabi-gcc is not on PATH, fall back to an Arm GNU Toolchain
# unpacked under ~/opt. Note Homebrew's arm-none-eabi-gcc ships without newlib
# and cannot build this project -- prefer the Arm release.
ARM_FALLBACK_BIN := $(firstword $(wildcard $(HOME)/opt/arm-gnu-toolchain-*/bin))
GNU_USABLE := $(shell echo '\#include <stdint.h>' | arm-none-eabi-gcc -E - > /dev/null 2>&1 && echo yes)

ifneq ($(GNU_USABLE),yes)
ifneq ($(ARM_FALLBACK_BIN),)
GNU_INSTALL_ROOT ?= $(ARM_FALLBACK_BIN)/
endif
endif

GNU_INSTALL_ROOT ?=
GNU_PREFIX       ?= arm-none-eabi

CC      := $(GNU_INSTALL_ROOT)$(GNU_PREFIX)-gcc
CXX     := $(GNU_INSTALL_ROOT)$(GNU_PREFIX)-g++
AS      := $(GNU_INSTALL_ROOT)$(GNU_PREFIX)-gcc
LD      := $(GNU_INSTALL_ROOT)$(GNU_PREFIX)-gcc
OBJCOPY := $(GNU_INSTALL_ROOT)$(GNU_PREFIX)-objcopy
OBJDUMP := $(GNU_INSTALL_ROOT)$(GNU_PREFIX)-objdump
SIZE    := $(GNU_INSTALL_ROOT)$(GNU_PREFIX)-size

SDK     := vendor/nRF5_SDK_14.2.0
DECA    := vendor/deca_driver
BOARDS  := vendor/boards

# --- sources ---------------------------------------------------------------

SRC_C := \
  src/main.c \
  src/app.c \
  src/cfg.c \
  src/timing.c \
  src/slots.c \
  src/radio.c \
  src/ranging.c \
  src/sync.c \
  src/role_initiator.c \
  src/role_responder.c \
  src/output.c \
  src/serial.c \
  src/uart_io.c \
  src/syscalls.c \
  $(DECA)/deca_device.c \
  $(DECA)/deca_params_init.c \
  $(DECA)/deca_range_tables.c \
  $(DECA)/port/port_platform.c \
  $(SDK)/components/boards/boards.c \
  $(SDK)/components/drivers_nrf/common/nrf_drv_common.c \
  $(SDK)/components/drivers_nrf/spi_master/nrf_drv_spi.c \
  $(SDK)/components/drivers_nrf/hal/nrf_nvmc.c \
  $(SDK)/components/libraries/util/app_error.c \
  $(SDK)/components/libraries/util/app_error_weak.c \
  $(SDK)/components/libraries/util/app_util_platform.c \
  $(SDK)/components/libraries/util/nrf_assert.c \
  $(SDK)/components/toolchain/system_nrf52.c

SRC_S := \
  $(SDK)/components/toolchain/gcc/gcc_startup_nrf52.S

INC_DIRS := \
  src \
  config \
  $(BOARDS) \
  $(DECA) \
  $(DECA)/port \
  $(SDK)/components/boards \
  $(SDK)/components/device \
  $(SDK)/components/drivers_nrf/common \
  $(SDK)/components/drivers_nrf/delay \
  $(SDK)/components/drivers_nrf/hal \
  $(SDK)/components/drivers_nrf/nrf_soc_nosd \
  $(SDK)/components/drivers_nrf/spi_master \
  $(SDK)/components/drivers_nrf/uart \
  $(SDK)/components/libraries/experimental_section_vars \
  $(SDK)/components/libraries/experimental_log \
  $(SDK)/components/libraries/experimental_log/src \
  $(SDK)/components/libraries/experimental_memobj \
  $(SDK)/components/libraries/balloc \
  $(SDK)/components/libraries/atomic \
  $(SDK)/components/libraries/strerror \
  $(SDK)/components/libraries/util \
  $(SDK)/components/toolchain \
  $(SDK)/components/toolchain/cmsis/include \
  $(SDK)/components/toolchain/gcc \
  $(SDK)/external/fprintf

# --- flags -----------------------------------------------------------------

CPU_FLAGS := -mcpu=cortex-m4 -mthumb -mabi=aapcs -mfloat-abi=hard -mfpu=fpv4-sp-d16

DEFINES := \
  -DBOARD_DW1001_DEV \
  -DBSP_DEFINES_ONLY \
  -DCONFIG_GPIO_AS_PINRESET \
  -DFLOAT_ABI_HARD \
  -DNRF52 \
  -DNRF52832_XXAA \
  -DNRF52_PAN_74 \
  -DSWI_DISABLE0

OPT := -O2 -g3

CFLAGS := $(CPU_FLAGS) $(DEFINES) $(OPT) \
  -std=gnu11 \
  -Wall -Wextra -Wno-unused-parameter -Wno-expansion-to-defined \
  -ffunction-sections -fdata-sections -fno-strict-aliasing \
  -fno-builtin -fshort-enums \
  $(addprefix -I,$(INC_DIRS))

# The vendored Decawave sources are kept byte-for-byte identical to upstream,
# so their warnings are silenced rather than fixed.
VENDOR_CFLAGS := -Wno-all -w

ASMFLAGS := $(CPU_FLAGS) $(DEFINES) -g3 -x assembler-with-cpp \
  -D__HEAP_SIZE=2048 -D__STACK_SIZE=8192 \
  $(addprefix -I,$(INC_DIRS))

LDSCRIPT := $(SDK)/components/toolchain/gcc/nrf52_xxaa.ld

LDFLAGS := $(CPU_FLAGS) \
  -T$(LDSCRIPT) -L$(SDK)/components/toolchain/gcc \
  -Wl,--gc-sections -Wl,-Map=$(OUTPUT_DIR)/$(PROJECT_NAME).map \
  --specs=nano.specs --specs=nosys.specs

LIBS := -lm

# --- objects ---------------------------------------------------------------

OBJ := $(addprefix $(OUTPUT_DIR)/,$(SRC_C:.c=.o)) \
       $(addprefix $(OUTPUT_DIR)/,$(SRC_S:.S=.o))

ELF := $(OUTPUT_DIR)/$(PROJECT_NAME).elf
HEX := $(OUTPUT_DIR)/$(PROJECT_NAME).hex
BIN := $(OUTPUT_DIR)/$(PROJECT_NAME).bin

.PHONY: all clean size flash flash-jl erase check-driver

all: $(HEX) $(BIN) size

$(OUTPUT_DIR)/$(DECA)/%.o: $(DECA)/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $(VENDOR_CFLAGS) -c $< -o $@

$(OUTPUT_DIR)/$(SDK)/%.o: $(SDK)/%.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) $(VENDOR_CFLAGS) -c $< -o $@

$(OUTPUT_DIR)/%.o: %.c
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -MMD -MP -c $< -o $@

$(OUTPUT_DIR)/%.o: %.S
	@mkdir -p $(dir $@)
	$(AS) $(ASMFLAGS) -c $< -o $@

$(ELF): $(OBJ)
	@mkdir -p $(dir $@)
	$(LD) $(LDFLAGS) $(OBJ) $(LIBS) -o $@

$(HEX): $(ELF)
	$(OBJCOPY) -O ihex $< $@

$(BIN): $(ELF)
	$(OBJCOPY) -O binary $< $@

size: $(ELF)
	@$(SIZE) $(ELF)

clean:
	rm -rf $(OUTPUT_DIR)

# Fails if anything under vendor/deca_driver has drifted from upstream.
check-driver:
	@shasum -a 256 -c vendor/deca_driver.sha256 >/dev/null && \
	  echo "deca_driver matches upstream Decawave/dwm1001-examples"

flash: $(HEX)
	nrfjprog -f nrf52 --program $(HEX) --sectorerase --verify
	nrfjprog -f nrf52 --reset

flash-jl: $(HEX)
	@printf 'device NRF52832_XXAA\nsi 1\nspeed 4000\nr\nh\nloadfile %s\nr\ng\nqc\n' \
	  "$(abspath $(HEX))" > $(OUTPUT_DIR)/flash.jlink
	JLinkExe -CommanderScript $(OUTPUT_DIR)/flash.jlink

erase:
	nrfjprog -f nrf52 --eraseall

-include $(shell find $(OUTPUT_DIR) -name '*.d' 2>/dev/null)
