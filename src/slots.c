#include "slots.h"
#include "timing.h"

uint32_t slot_resp_hi32(const cfg_t *c, uint32_t poll_rx_hi32, uint8_t slot)
{
    /* Equation (1). */
    return poll_rx_hi32
         + us_to_hi32(c->resp_base_us + (uint32_t)slot * c->resp_pitch_us);
}

uint32_t slot_report_hi32(const cfg_t *c, uint32_t final_rx_hi32, uint8_t slot)
{
    /* Equation (3). */
    return final_rx_hi32
         + us_to_hi32(c->report_base_us + (uint32_t)slot * c->report_pitch_us);
}

uint32_t slot_final_hi32(const cfg_t *c, uint32_t poll_hi32,
                         uint32_t last_resp_rx_hi32, int32_t last_slot)
{
    if (last_slot == (int32_t)(c->resp_count - 1u))
    {
        /* Equation (2). */
        return last_resp_rx_hi32 + us_to_hi32(c->resp_pitch_us);
    }

    /* Nominal instant: see the note in slots.h. */
    return poll_hi32
         + us_to_hi32(c->resp_base_us
                      + (uint32_t)c->resp_count * c->resp_pitch_us);
}

uint32_t slot_resp_window_end_hi32(const cfg_t *c, uint32_t poll_hi32)
{
    return poll_hi32
         + us_to_hi32(c->resp_base_us
                      + (uint32_t)(c->resp_count - 1u) * c->resp_pitch_us)
         + ns_to_hi32(timing_air_resp_ns(c))
         + us_to_hi32(c->guard_us / 2u);
}

uint32_t slot_report_window_end_hi32(const cfg_t *c, uint32_t final_hi32)
{
    return final_hi32
         + us_to_hi32(c->report_base_us
                      + (uint32_t)(c->resp_count - 1u) * c->report_pitch_us)
         + ns_to_hi32(timing_air_report_ns(c))
         + us_to_hi32(c->guard_us / 2u);
}

uint32_t slot_exchange_deadline_hi32(const cfg_t *c, uint32_t poll_rx_hi32)
{
    /* The nominal Final instant is an RMARKER, but radio_rx() does not return
     * until the whole frame has arrived, so the Final's own air time has to be
     * inside the deadline. Without it, at 110 kbps the Final needs ~3.9 ms of
     * air after its RMARKER against a 900 us rx_timeout_us, so every exchange
     * is reaped before its Final can land and DS-TWR never completes. */
    return poll_rx_hi32
         + us_to_hi32(c->resp_base_us
                      + (uint32_t)c->resp_count * c->resp_pitch_us
                      + c->rx_timeout_us)
         + ns_to_hi32(timing_air_final_ns(c));
}
