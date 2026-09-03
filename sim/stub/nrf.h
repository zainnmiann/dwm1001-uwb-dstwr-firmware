/* Host stub. cfg.c includes nrf.h but the simulator never reaches the flash
 * paths (cfg_load/cfg_save are not called; cfg_defaults() is used directly),
 * so nothing here needs to do anything. */
#ifndef NRF_H_STUB_
#define NRF_H_STUB_

#include <stdlib.h>

/* serial.c's $RESET reaches for this. The harness never issues console
 * commands, so it is unreachable there; abort loudly rather than silently
 * pretending a reset happened. */
static inline void NVIC_SystemReset(void) { abort(); }

#endif
