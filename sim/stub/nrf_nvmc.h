/* Host stub for the flash driver. Declared so cfg.c compiles; the simulator
 * never calls cfg_save(), which is the only caller. */
#ifndef NRF_NVMC_H_STUB_
#define NRF_NVMC_H_STUB_
#include <stdint.h>
void nrf_nvmc_page_erase(uint32_t addr);
void nrf_nvmc_write_words(uint32_t addr, const uint32_t *src, uint32_t n);
#endif
