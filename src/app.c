#include "app.h"
#include "radio.h"

static stats_t m_stats;
static bool    m_ranging;

stats_t *app_stats(void)       { return &m_stats; }
void     app_stats_clear(void) { __builtin_memset(&m_stats, 0, sizeof(m_stats)); }
bool     app_ranging(void)     { return m_ranging; }
void     app_ranging_set(bool on) { m_ranging = on; }

void app_stat_proc(uint32_t *last, uint32_t *max, uint32_t start_hi32)
{
    int32_t d = radio_hi32_diff_us(radio_systime_hi32(), start_hi32);

    if (d < 0) { d = 0; }
    *last = (uint32_t)d;
    if ((uint32_t)d > *max) { *max = (uint32_t)d; }
}
