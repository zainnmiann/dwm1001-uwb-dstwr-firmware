/**
 * @file    node_stubs.c
 * @brief   The last few target entry points the linked firmware reaches for.
 *
 * serial.c's banner reads the DW1000 identity directly, and cfg.c writes the
 * parameter set to flash. Neither is reachable in the harness -- there is no
 * console input, so nothing calls $SAVE -- but both must resolve.
 */
#include "deca_device_api.h"

#include <stdint.h>

uint32 dwt_getpartid(void)  { return 0xD0000001u; }
uint32 dwt_readdevid(void)  { return DWT_DEVICE_ID; }

void nrf_nvmc_page_erase(uint32_t addr)
{
    (void)addr;
}

void nrf_nvmc_write_words(uint32_t addr, const uint32_t *src, uint32_t n)
{
    (void)addr; (void)src; (void)n;
}
