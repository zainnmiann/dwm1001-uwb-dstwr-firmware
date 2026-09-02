/**
 * @file    sim_radio.c
 * @brief   The parts of radio.h and the nRF drivers that sync.c and cfg.c
 *          reference, implemented against the virtual clock.
 *
 * Only the entry points the portable modules actually call are provided. The
 * frame schedule itself is driven by sim_main.c rather than by the blocking
 * role loops, so radio_rx()/radio_tx_at() are deliberately absent -- see the
 * scope note in sim_main.c.
 */
#include "sim.h"
#include "radio.h"

static const nclk_t *s_clk;
static int64_t       s_now;

void sim_bind_clock(const nclk_t *c, int64_t now_dtu)
{
    s_clk = c;
    s_now = now_dtu;
}

int64_t sim_global_from_hi32(const nclk_t *c, uint32_t hi32, int64_t after)
{
    /* Lift the 32-bit view back onto the full timeline using `after` as the
     * epoch, then invert the clock. */
    int64_t  base = sim_local_dtu(c, after);
    uint64_t want = ((uint64_t)hi32) << 8;
    uint64_t have = (uint64_t)base & DTU_MASK_40;
    int64_t  delta = (int64_t)((want - (have & ~0xFFULL)) & 0xFFFFFFFF00ULL);

    return sim_global_from_local(c, base + delta);
}

uint32_t radio_systime_hi32(void)
{
    return (uint32_t)((uint64_t)sim_local_dtu(s_clk, s_now) >> 8);
}

int32_t radio_hi32_diff_us(uint32_t a, uint32_t b)
{
    /* Identical to the target implementation: one hi32 unit is 4.0064 ns. */
    int32_t d = (int32_t)(a - b);
    return (int32_t)(((int64_t)d * 1000LL) / 249600LL);
}

/* --- stubs for entry points the portable modules reference but never reach - */
void nrf_nvmc_page_erase(uint32_t addr)                              { (void)addr; }
void nrf_nvmc_write_words(uint32_t a, const uint32_t *s, uint32_t n) { (void)a; (void)s; (void)n; }
bool uart_io_baud_supported(uint32_t baud)                           { (void)baud; return true; }
